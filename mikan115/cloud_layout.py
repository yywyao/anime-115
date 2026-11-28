"""Publish verified task files into owned series folders without overwriting.

Cloud jobs keep their exclusive staging folder until naming has completed.
Every move is journaled by 115 file ID so a timed-out response can be reconciled.
"""
import json
import posixpath
import secrets
import time
import unicodedata

from .errors import AppError
from .organizer import clean


SCHEMA='''
CREATE TABLE IF NOT EXISTS cloud_series (
 series_key TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, cid TEXT, pending_name TEXT, create_started INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cloud_layouts (
 transfer_id TEXT PRIMARY KEY REFERENCES transfers(id), series_key TEXT NOT NULL REFERENCES cloud_series(series_key),
 stage_cid TEXT NOT NULL, stage_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'WAITING',
 error TEXT, cleaned INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_outputs (
 transfer_id TEXT NOT NULL REFERENCES transfers(id), file_id TEXT NOT NULL REFERENCES transfer_files(id),
 remote_id TEXT NOT NULL, source_cid TEXT NOT NULL, old_path TEXT NOT NULL, new_path TEXT NOT NULL,
 name TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PREPARED',
 PRIMARY KEY(transfer_id,file_id));
'''


def normalized(name):return unicodedata.normalize('NFKC',name).casefold()


class CloudLayout:
    def __init__(self,store,workflow,catalog):
        self.store,self.workflow,self.catalog=store,workflow,catalog
        with store.transaction() as db:
            db.executescript(SCHEMA)
            columns={r['name'] for r in db.execute('PRAGMA table_info(cloud_series)')}
            if 'pending_name' not in columns:db.execute('ALTER TABLE cloud_series ADD COLUMN pending_name TEXT')
            if 'create_started' not in columns:db.execute('ALTER TABLE cloud_series ADD COLUMN create_started INTEGER NOT NULL DEFAULT 0')

    def root(self,pan,binding):
        """Only rename the bound root, never adopt an unrelated mikan folder."""
        if binding['root_name']=='mikan':
            pan.verify_child('0',binding['root_cid'],'mikan');return binding
        old='Mikan115-Transfers-'+self.store.instance_id[:8]
        if binding['root_name']!=old:raise AppError('FOLDER_SCOPE_MISMATCH','转存目录未确认',409)
        rows=pan.list_files('0')
        same=[r for r in rows if not r.get('fid') and str(r.get('cid'))==binding['root_cid']]
        if len(same)!=1:raise AppError('FOLDER_SCOPE_MISMATCH','转存目录已变化',409)
        name=str(same[0].get('n') or same[0].get('name'))
        if name not in {old,'mikan'}:raise AppError('FOLDER_SCOPE_MISMATCH','转存目录已变化',409)
        if any(normalized(str(r.get('n') or r.get('name') or ''))=='mikan' and str(r.get('cid'))!=binding['root_cid'] for r in rows):
            raise AppError('CLOUD_ROOT_CONFLICT','已存在其他 mikan 目录，请先更名后重试',409)
        # The persisted intent distinguishes a completed rename from external changes.
        intent=self.store.setting('cloud_root_rename')
        if name=='mikan' and intent!=binding['root_cid']:raise AppError('FOLDER_SCOPE_MISMATCH','转存目录已变化',409)
        self.store.set_setting('cloud_root_rename',binding['root_cid'])
        if name==old:pan.rename_file(binding['root_cid'],'mikan')
        pan.verify_child('0',binding['root_cid'],'mikan')
        with self.store.transaction() as db:
            db.execute("UPDATE binding SET root_name='mikan',updated_at=? WHERE id=1 AND root_cid=?",(time.time(),binding['root_cid']))
            db.execute("DELETE FROM settings WHERE key='cloud_root_rename'")
        return self.store.binding(False)

    def identity(self,task):
        row=self.store.one('SELECT metadata FROM transfer_naming WHERE transfer_id=?',(task['id'],))
        meta=json.loads(row['metadata']) if row else {}
        ident=meta.get('work_id') or task['work_id']
        if not ident:return None
        work=self.catalog.work(ident)
        scope=self.catalog.tmdb_scope(work)
        if scope and (work.get('tmdb_mapping') or {}).get('status') in {'linked','episode_scope'}:
            key='tmdb:'+scope[0]+':'+str(scope[1])
            # TMDB's parent title is distinct from a season/episode display title.
            title=work.get('series_title') or work.get('title') or work.get('title_original')
        else:
            bid=str(meta.get('bangumi_id') or work.get('mikan_id') or '')
            key='mikan:'+bid if bid.isdigit() else 'work:'+str(ident)
            title=work.get('title') or work.get('cleanTitle')
        if not title:return None
        return key,clean(title,100)

    def row(self,tid):
        return self.store.one('''SELECT l.*,s.name AS series_name,s.cid AS series_cid FROM cloud_layouts l
            JOIN cloud_series s ON s.series_key=l.series_key WHERE l.transfer_id=?''',(tid,))

    def ready(self,tid):
        row=self.row(tid)
        if row:return row['status']=='PUBLISHED'
        task=self.store.one('SELECT work_id FROM transfers WHERE id=?',(tid,))
        # Unassociated, manually entered magnets retain the legacy isolated layout.
        return not task or not task['work_id']

    def prepare(self,task,pan,binding):
        row=self.row(task['id'])
        if row:return row
        identity=self.identity(task)
        if not identity:return None
        key,title=identity
        # Serialize folder reservations across worker processes, not only threads.
        with self.store.transaction() as db:
            series=db.execute('SELECT * FROM cloud_series WHERE series_key=?',(key,)).fetchone()
            if not series:
                rows=pan.list_files(binding['root_cid'])
                occupied={normalized(str(r.get('n') or r.get('name') or '')) for r in rows}
                occupied.update(normalized(r['name']) for r in db.execute('SELECT name FROM cloud_series'))
                name=title
                if normalized(name) in occupied:name=clean(title,75)+' ['+clean(key,35)+']'
                if normalized(name) in occupied:raise AppError('SERIES_FOLDER_CONFLICT','系列目录重名，请核对后重试',409)
                pending='.mikan-'+secrets.token_hex(16)
                db.execute('INSERT INTO cloud_series(series_key,name,pending_name) VALUES(?,?,?)',(key,name,pending))
        self.ensure_series(pan,binding,key)
        # AList resolves a new path through its cached parent listing. Refresh
        # that listing first after direct 115 directory creation/rename.
        self.workflow.alist.list_files(binding['mount_path'])
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO cloud_layouts(transfer_id,series_key,stage_cid,stage_path,updated_at) VALUES(?,?,?,?,?)',
                       (task['id'],key,task['task_cid'],task['task_path'],time.time()))
        return self.row(task['id'])

    def ensure_series(self,pan,binding,key):
        """Reserve a random marker before POST; recover the same folder after timeout."""
        rows=pan.list_files(binding['root_cid']);start=False
        with self.store.transaction() as db:
            series=dict(db.execute('SELECT * FROM cloud_series WHERE series_key=?',(key,)).fetchone())
            if not series['cid']:
                matching=[r for r in rows if not r.get('fid') and str(r.get('n') or r.get('name'))==series['pending_name']]
                if len(matching)>1:raise AppError('SERIES_FOLDER_CONFLICT','系列临时目录重复，已停止整理',409)
                if matching:
                    series['cid']=str(matching[0]['cid'])
                    db.execute('UPDATE cloud_series SET cid=? WHERE series_key=?',(series['cid'],key))
                elif series['create_started']:
                    raise AppError('SERIES_FOLDER_PENDING','系列目录创建结果待确认，请稍后重试',409)
                else:
                    db.execute('UPDATE cloud_series SET create_started=1 WHERE series_key=?',(key,));start=True
        if start:
            try:cid=pan.create_folder(binding['root_cid'],series['pending_name'])
            except AppError as error:
                if not error.uncertain and error.code in {'PAN115_REJECTED','COOKIE_EXPIRED','INVALID_FOLDER_NAME'}:
                    with self.store.transaction() as db:db.execute('UPDATE cloud_series SET create_started=0 WHERE series_key=? AND cid IS NULL',(key,))
                raise
            with self.store.transaction() as db:db.execute('UPDATE cloud_series SET cid=? WHERE series_key=?',(cid,key))
            series['cid']=cid
        # Hold the write lock while renaming so concurrent tasks in this series coalesce.
        with self.store.transaction() as db:
            rows=pan.list_files(binding['root_cid'])
            same=[r for r in rows if not r.get('fid') and str(r.get('cid'))==series['cid']]
            if len(same)!=1:raise AppError('FOLDER_SCOPE_MISMATCH','系列目录已变化',409)
            current=str(same[0].get('n') or same[0].get('name'))
            if current not in {series['name'],series['pending_name']}:raise AppError('FOLDER_SCOPE_MISMATCH','系列目录已变化',409)
            if any(normalized(str(r.get('n') or r.get('name') or ''))==normalized(series['name']) and str(r.get('cid'))!=series['cid'] for r in rows):
                raise AppError('SERIES_FOLDER_CONFLICT','系列目录重名，已停止整理',409)
            if current!=series['name']:pan.rename_file(series['cid'],series['name'])
            pan.verify_child(binding['root_cid'],series['cid'],series['name'])
            db.execute('UPDATE cloud_series SET pending_name=NULL WHERE series_key=?',(key,))

    def scope(self,task,pan,binding,row):
        pan.verify_child(binding['root_cid'],row['series_cid'],row['series_name'])
        expected=self.workflow.alist.safe_path(binding['mount_path'],row['series_name'])
        if task['task_cid']!=row['series_cid'] or task['task_path']!=expected:
            raise AppError('TASK_SCOPE_MISMATCH','系列目录未通过校验',409)

    def check_file(self,task,pan,file,row):
        op=self.store.one("SELECT * FROM cloud_outputs WHERE transfer_id=? AND file_id=? AND status='DONE'",(task['id'],file['id']))
        if not op or file['path']!=op['new_path'] or file['size']!=op['size']:raise AppError('FILE_SCOPE_CHANGED','文件未通过任务校验',409)
        matches=[r for r in pan.list_files(row['series_cid']) if str(r.get('fid'))==op['remote_id']]
        if len(matches)!=1 or str(matches[0].get('n') or matches[0].get('name'))!=op['name'] or int(matches[0].get('s',-1))!=op['size']:
            raise AppError('FILE_SCOPE_CHANGED','文件已变化，请刷新后重试',409)

    def publish(self,task):
        with self.workflow.connection_lock:
            binding,pan=self.workflow.scope(task)
            row=self.prepare(task,pan,binding)
            if not row:return
            if row['status']=='PUBLISHED':
                self.cleanup(pan,binding,row)
                self.workflow.update(task['id'],error_code=None,error_message=None)
                return
            pan.verify_child(binding['root_cid'],row['series_cid'],row['series_name'])
            prefix=self.workflow.alist.safe_path(binding['mount_path'],row['series_name'])
            files=self.store.all('SELECT * FROM transfer_files WHERE transfer_id=? ORDER BY path',(task['id'],))
            if not files:return
            if not self.store.one('SELECT 1 FROM cloud_outputs WHERE transfer_id=?',(task['id'],)):
                plans=[];names=set();existing={normalized(str(r.get('n') or r.get('name') or '')) for r in pan.list_files(row['series_cid'])}
                for file in files:
                    if not file['path'].startswith(row['stage_path']+'/'):raise AppError('UNSAFE_PATH','文件超出任务目录',403)
                    name=posixpath.basename(file['path']);key=normalized(name)
                    if key in names or key in existing:raise AppError('PUBLISH_COLLISION','系列中已有同名文件，未覆盖，请调整文件名后重试',409)
                    names.add(key)
                    parent,remote=pan.locate_file(row['stage_cid'],file['path'][len(row['stage_path'])+1:])
                    if int(remote.get('s',-1))!=file['size']:raise AppError('FILE_SCOPE_CHANGED','文件大小已变化',409)
                    plans.append((task['id'],file['id'],str(remote['fid']),parent,file['path'],prefix+'/'+name,name,file['size']))
                with self.store.transaction() as db:
                    db.executemany('INSERT INTO cloud_outputs(transfer_id,file_id,remote_id,source_cid,old_path,new_path,name,size) VALUES(?,?,?,?,?,?,?,?)',plans)
            ops=self.store.all('SELECT * FROM cloud_outputs WHERE transfer_id=?',(task['id'],))
            if {p['file_id'] for p in ops}!={f['id'] for f in files}:raise AppError('PUBLISH_PLAN_CHANGED','文件清单已变化，请核对后重试',409)
            for op in ops:
                self.workflow.update(task['id'],lease_until=time.time()+180)
                self.move(pan,binding,row,op)
            with self.store.transaction() as db:
                for op in ops:db.execute('UPDATE transfer_files SET path=?,name=? WHERE id=? AND transfer_id=?',(op['new_path'],op['name'],op['file_id'],task['id']))
                db.execute('UPDATE transfers SET task_cid=?,task_path=?,updated_at=? WHERE id=?',(row['series_cid'],prefix,time.time(),task['id']))
                db.execute("UPDATE cloud_layouts SET status='PUBLISHED',error=NULL,updated_at=? WHERE transfer_id=?",(time.time(),task['id']))
                db.execute('UPDATE transfers SET error_code=NULL,error_message=NULL WHERE id=?',(task['id'],))
            self.workflow.alist.list_files(binding['mount_path'])
            self.workflow.alist.list_files(prefix)
            self.store.event(task['id'],'FILES_ORGANIZED','已整理至系列目录')
            self.cleanup(pan,binding,self.row(task['id']))

    def move(self,pan,binding,row,op):
        pan.verify_child(binding['root_cid'],row['series_cid'],row['series_name'])
        targets=pan.list_files(row['series_cid'])
        same=[r for r in targets if str(r.get('fid'))==op['remote_id']]
        conflict=[r for r in targets if normalized(str(r.get('n') or r.get('name') or ''))==normalized(op['name']) and str(r.get('fid'))!=op['remote_id']]
        if conflict:raise AppError('PUBLISH_COLLISION','系列中已有同名文件，未覆盖',409)
        if not same:
            parent,rows=pan.file_parent(row['stage_cid'],op['old_path'][len(row['stage_path'])+1:])
            if parent!=op['source_cid']:raise AppError('FILE_SCOPE_CHANGED','原目录已变化',409)
            same=[r for r in rows if str(r.get('fid'))==op['remote_id']]
            if len(same)!=1 or str(same[0].get('n') or same[0].get('name'))!=op['name'] or int(same[0].get('s',-1))!=op['size']:
                raise AppError('FILE_SCOPE_CHANGED','原文件未通过校验',409)
            pan.move_file(op['remote_id'],row['series_cid'])
            same=[r for r in pan.list_files(row['series_cid']) if str(r.get('fid'))==op['remote_id']]
        if len(same)!=1 or str(same[0].get('n') or same[0].get('name'))!=op['name'] or int(same[0].get('s',-1))!=op['size']:
            raise AppError('PUBLISH_PENDING','整理结果待确认，将自动核对',409)
        with self.store.transaction() as db:db.execute("UPDATE cloud_outputs SET status='DONE' WHERE transfer_id=? AND file_id=?",(op['transfer_id'],op['file_id']))

    def cleanup(self,pan,binding,row):
        if row['cleaned']:return
        # Never remove a non-empty or externally changed folder.
        def empty(parent,cid,name,depth=0):
            if depth>8:raise AppError('DIRECTORY_TOO_DEEP','临时目录层级过深',409)
            pan.verify_child(parent,cid,name)
            rows=pan.list_files(cid)
            if any(r.get('fid') for r in rows):raise AppError('FOLDER_NOT_EMPTY','临时目录有其他文件，已保留',409)
            for child in rows:empty(cid,str(child['cid']),str(child.get('n') or child.get('name')),depth+1)
            pan.remove_empty_folder(parent,cid,name)
        children=pan.list_files(binding['root_cid'])
        exists=[r for r in children if not r.get('fid') and str(r.get('cid'))==row['stage_cid']]
        if exists:empty(binding['root_cid'],row['stage_cid'],row['stage_path'].rsplit('/',1)[-1])
        with self.store.transaction() as db:db.execute('UPDATE cloud_layouts SET cleaned=1,error=NULL WHERE transfer_id=?',(row['transfer_id'],))
        self.workflow.alist.list_files(binding['mount_path'])

    def tick(self):
        # Naming review is resolved explicitly rather than inventing episode numbers.
        tasks=self.store.all('''SELECT t.* FROM transfers t LEFT JOIN transfer_naming n ON n.transfer_id=t.id
            LEFT JOIN cloud_layouts l ON l.transfer_id=t.id WHERE t.state='FILES_READY' AND t.work_id!=''
            AND (n.auto_rename IS NOT 1 OR n.status='DONE') AND (l.transfer_id IS NULL OR l.status!='PUBLISHED' OR l.cleaned=0)
            ORDER BY t.created_at LIMIT 5''')
        for task in tasks:
            claimed=self.workflow.claim(task['id'],{'FILES_READY'})
            if not claimed:continue
            try:self.publish(claimed)
            except AppError as error:
                with self.store.transaction() as db:db.execute("UPDATE cloud_layouts SET error=?,updated_at=? WHERE transfer_id=?",(error.message,time.time(),task['id']))
                self.workflow.update(task['id'],error_code=error.code,error_message=error.message)
            finally:self.workflow.update(task['id'],lease_until=0,lease_owner=None)
