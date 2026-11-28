"""Persistent, isolated downloads from confirmed Alist transfer files.

This queue deliberately accepts file IDs, never caller-supplied paths.  The
workflow remains the authority for ownership, binding scope, and the Alist
stream; this module only stages a verified stream into the local library.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import time
import datetime
import unicodedata

from .errors import AppError
from . import releases


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS library_tasks (
 id TEXT PRIMARY KEY, transfer_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
 status TEXT NOT NULL, root_path TEXT NOT NULL, metadata TEXT NOT NULL,
 expected_episodes TEXT NOT NULL, aired_episodes TEXT NOT NULL DEFAULT '[]', episodes TEXT NOT NULL, missing_episodes TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
 lease_owner TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS library_items (
 id TEXT PRIMARY KEY, library_task_id TEXT NOT NULL REFERENCES library_tasks(id),
 file_id TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
 target_path TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
 UNIQUE(library_task_id,file_id));
CREATE INDEX IF NOT EXISTS library_tasks_transfer ON library_tasks(transfer_id);
CREATE TABLE IF NOT EXISTS library_requests (
 transfer_id TEXT NOT NULL REFERENCES transfers(id), user_id INTEGER NOT NULL REFERENCES users(id),
 status TEXT NOT NULL DEFAULT 'WAITING', error TEXT, next_retry REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(transfer_id,user_id));
'''


def _episodes(name, confirmed_season=None):
    """Return explicit (season, episode) pairs; never infer a season."""
    text=Path(name).name
    lowered=text.lower()
    if re.search(r'(?i)\b(?:op|ed|opening|ending|sample|preview|pv)\b',lowered):return set()
    season=None
    match=re.search(r'(?i)\bS(\d{1,2})\s*[-_. ]?E(\d{1,4})\b',text)
    if match:
        season,episode=int(match[1]),int(match[2])
    else:
        match=re.search(r'(?i)\bepisode\s*[-_. ]?(\d{1,4})\b',text)
        if match:
            episode=int(match[1]);season=confirmed_season
        else:
            match=re.search(r'(?i)\b(?:EP?|E)[ ._-]?(\d{1,4})\b',text)
            if not match:
                match=re.search(r'(?:第\s*)(\d{1,4})(?:\s*[集话話])',text)
            if not match:
                parsed=releases.parse(text)
                parsed_episodes=parsed.get('episodes') or []
                return {(confirmed_season,n) for n in parsed_episodes if isinstance(n,int) and n>0}
            episode=int(match[1]);season=confirmed_season
    if episode<1:return set()
    if confirmed_season is not None and season is not None and season!=confirmed_season:return set()
    return {(season,episode)}


def _is_video(name):
    return Path(name).suffix.lower() in {'.mkv','.mp4','.avi','.mov','.webm','.ts','.m2ts','.flv','.wmv'}


class Library:
    def __init__(self,store,workflow,root=None,catalog=None):
        self.store,self.workflow,self.catalog=store,workflow,catalog
        base=Path(store.data).resolve()
        target=(base/'library' if root is None else Path(root).resolve())
        if not target.is_relative_to(base):
            raise AppError('INVALID_LIBRARY_ROOT','入库目录必须位于应用数据目录内',503)
        self.root=target
        self.root.mkdir(parents=True,exist_ok=True)
        with self.store.transaction() as db:
            db.executescript(_SCHEMA)
            columns={row['name'] for row in db.execute('PRAGMA table_info(library_tasks)')}
            if 'aired_episodes' not in columns:db.execute("ALTER TABLE library_tasks ADD COLUMN aired_episodes TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def _user_id(user):
        try:value=int(user['id'])
        except (KeyError,TypeError,ValueError):raise AppError('AUTH_REQUIRED','请先登录',401)
        if value<=0:raise AppError('AUTH_REQUIRED','请先登录',401)
        return value

    @staticmethod
    def _safe_name(name):
        if not isinstance(name,str) or not name or '\0' in name or '\\' in name:
            raise AppError('UNSAFE_LIBRARY_NAME','文件名不安全，未写入本地目录',409)
        path=PurePosixPath(name)
        if path.is_absolute() or any(part in {'','.','..'} for part in path.parts):
            raise AppError('UNSAFE_LIBRARY_NAME','文件名不安全，未写入本地目录',409)
        return path

    def _destination(self,tid,task_id):
        layout=getattr(self.workflow,'layout',None)
        series=layout.row(tid) if layout else None
        if series and series['status']=='PUBLISHED':
            name=str(series['series_name'])
            if self._safe_name(name).parts!=(name,):raise AppError('UNSAFE_LIBRARY_PATH','系列目录无效',409)
            target=(self.root/'mikan'/name).resolve()
        else:target=(self.root/'mikan'/'未分类'/task_id).resolve()
        if not target.is_relative_to(self.root):raise AppError('UNSAFE_LIBRARY_PATH','入库目录不安全',503)
        return target

    @staticmethod
    def _reserve_targets(db,items):
        """A shared series directory cannot adopt another task's equal-size file."""
        existing={unicodedata.normalize('NFKC',str(Path(r['target_path']).resolve())).casefold()
                  for r in db.execute('SELECT target_path FROM library_items')}
        for item in items:
            target=Path(item[5]);key=unicodedata.normalize('NFKC',str(target.resolve())).casefold()
            if key in existing or target.exists():raise AppError('LIBRARY_FILE_CONFLICT','系列中已有同名文件，未覆盖，请核对文件名',409)
            existing.add(key)

    def _task_row(self,library_id):
        return self.store.one('SELECT * FROM library_tasks WHERE id=?',(library_id,))

    def _authorize(self,user,row):
        if not row:raise AppError('LIBRARY_TASK_NOT_FOUND','入库任务不存在',404)
        self.workflow.get(user,row['transfer_id'])
        return row

    @staticmethod
    def _following_valid(db,owner,following):
        if following is None:return True
        if not isinstance(following,(tuple,list)) or len(following)!=2:return False
        subscription_id,revision=str(following[0]),following[1]
        try:revision=int(revision)
        except (TypeError,ValueError):return False
        row=db.execute('''SELECT s.enabled,s.removed,s.revision,u.disabled,
                                 COALESCE((SELECT value FROM settings WHERE key='rss_paused'),'0') AS paused
                          FROM subscriptions s JOIN users u ON u.id=s.user_id
                          WHERE s.id=? AND s.user_id=?''',(subscription_id,owner)).fetchone()
        return bool(row and row['enabled'] and not row['removed'] and not row['disabled']
                    and int(row['revision'])==revision and row['paused']!='1')

    def _extend_existing(self,user,row,source_files,ids,owner,following):
        existing=self.store.all('SELECT file_id,name,size,target_path,status FROM library_items WHERE library_task_id=?',(row['id'],))
        present={str(item['file_id']) for item in existing};new_ids=[file_id for file_id in ids if file_id not in present]
        if not new_ids:
            if row['status']=='FAILED':
                with self.store.transaction() as db:
                    if not self._following_valid(db,owner,following):return None
                    db.execute("UPDATE library_items SET status='QUEUED',error='' WHERE library_task_id=? AND status='FAILED'",(row['id'],))
                    db.execute("UPDATE library_tasks SET status='QUEUED',error='',lease_until=0,lease_owner=NULL,updated_at=? WHERE id=?",(time.time(),row['id']))
            return self.task(user,row['id'])
        if row['status']=='DOWNLOADING':raise AppError('LIBRARY_TASK_BUSY','入库任务正在处理，请稍后再选择文件',409)
        root=Path(row['root_path']).resolve()
        if not root.is_relative_to(self.root):raise AppError('UNSAFE_LIBRARY_PATH','入库目录不安全',503)
        taken={str(Path(item['target_path']).resolve()) for item in existing};items=[];pairs=set()
        metadata=json.loads(row['metadata'] or '{}');season=metadata.get('season')
        for file_id in new_ids:
            file=source_files[file_id];relative=self._safe_name(str(file.get('name') or ''))
            try:size=int(file.get('size'))
            except (TypeError,ValueError):raise AppError('LIBRARY_SIZE_INVALID','文件大小未确认',409)
            target=str((root/relative).resolve())
            if not Path(target).is_relative_to(root) or target in taken:raise AppError('LIBRARY_FILE_CONFLICT','本地入库文件路径冲突',409)
            taken.add(target)
            if _is_video(str(relative)):pairs.update(_episodes(str(relative),season))
            items.append((secrets.token_hex(16),row['id'],file_id,str(relative),size,target,'QUEUED',''))
        episodes=set(json.loads(row['episodes'] or '[]'));episodes.update(ep for _,ep in pairs)
        with self.store.transaction() as db:
            if not self._following_valid(db,owner,following):return None
            current=db.execute('SELECT status,episodes,aired_episodes FROM library_tasks WHERE id=?',(row['id'],)).fetchone()
            if not current:return None
            if current['status']=='DOWNLOADING':raise AppError('LIBRARY_TASK_BUSY','入库任务正在处理，请稍后再选择文件',409)
            already={str(item['file_id']) for item in db.execute('SELECT file_id FROM library_items WHERE library_task_id=?',(row['id'],)).fetchall()}
            items=[item for item in items if str(item[2]) not in already]
            if items:
                self._reserve_targets(db,items)
                db.executemany('INSERT INTO library_items VALUES(?,?,?,?,?,?,?,?)',items)
                status='QUEUED' if current['status'] in {'COMPLETE','FAILED'} else current['status']
                missing=sorted(set(json.loads(current['aired_episodes'] or '[]'))-episodes)
                db.execute('UPDATE library_tasks SET status=?,episodes=?,missing_episodes=?,error="",lease_until=0,lease_owner=NULL,updated_at=? WHERE id=?',
                           (status,json.dumps(sorted(episodes)),json.dumps(missing),time.time(),row['id']))
        return self.task(user,row['id'])

    def enqueue(self,user,tid,data=None,*,following=None):
        data=data or {}
        if not isinstance(data,dict):raise AppError('LIBRARY_REQUEST_INVALID','入库参数不正确',400)
        if any(key in data for key in ('expected_episodes','target_path','name')):
            raise AppError('LIBRARY_REQUEST_INVALID','预期集数、目录和文件名由目录与资源决定',400)
        owner=self._user_id(user)
        source=self.workflow.get(user,tid)
        layout=getattr(self.workflow,'layout',None)
        if layout and not layout.ready(tid):raise AppError('FILES_ORGANIZING','文件整理完成后将开始下载',409)
        work_id=str(source.get('work_id') or '')
        naming_row=self.store.one('SELECT metadata FROM transfer_naming WHERE transfer_id=?',(str(tid),))
        naming={}
        if naming_row:
            try:naming=json.loads(naming_row['metadata'] or '{}')
            except (TypeError,ValueError):naming={}
        source_id=str(naming.get('bangumi_id') or '') if str(naming.get('bangumi_id') or '').isdigit() else ''
        if not source_id and work_id.isdigit():
            infohash=str(source.get('infohash') or '')
            linked=self.store.one('SELECT 1 AS yes FROM release_sources WHERE resource_hash=? AND bangumi_id=? LIMIT 1',(infohash,work_id)) if infohash else None
            if linked:source_id=work_id
        if not source_id and work_id and not work_id.isdigit() and self.catalog is not None:
            try:
                confirmed=self.catalog.work(work_id) or {}
                candidate=str(confirmed.get('mikan_id') or confirmed.get('bangumiId') or
                              (confirmed.get('provider_ids') or {}).get('bangumi') or '')
                infohash=str(source.get('infohash') or '')
                linked=self.store.one('SELECT 1 AS yes FROM release_sources WHERE resource_hash=? AND bangumi_id=? LIMIT 1',(infohash,candidate)) if infohash and candidate.isdigit() else None
                if linked:source_id=candidate
            except Exception:source_id=''
        mikan_id=source_id if source_id.isdigit() else ''
        if 'mikan_id' in data and str(data.get('mikan_id') or '')!=mikan_id:
            raise AppError('LIBRARY_MIKAN_ID_REQUIRED','入库必须关联已确认的蜜柑作品',409)
        source_files={str(f['id']):f for f in source.get('files') or [] if f.get('id') is not None}
        requested=data.get('file_ids')
        if requested is None:requested=list(source_files)
        if not isinstance(requested,list) or not requested:
            raise AppError('LIBRARY_FILES_REQUIRED','请选择要入库的文件',400)
        ids=[str(value) for value in requested]
        if len(set(ids))!=len(ids) or any(value not in source_files for value in ids):
            raise AppError('LIBRARY_FILE_ID_INVALID','入库文件未通过任务校验',409)
        existing=self.store.one('SELECT * FROM library_tasks WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1',(str(tid),))
        if existing:return self._extend_existing(user,existing,source_files,ids,owner,following)
        season,expected,aired=self._catalog_episodes(mikan_id)
        task_id=secrets.token_hex(16);now=time.time();root=self._destination(tid,task_id)
        if not root.is_relative_to(self.root):raise AppError('UNSAFE_LIBRARY_PATH','入库目录不安全',503)
        metadata={'mikan_id':mikan_id,'title':str(source.get('title') or ''),'season':season,
                  'transfer_id':str(tid),'release_hash':str(naming.get('resource_hash') or source.get('infohash') or ''),
                  'owner_is_admin':bool(user.get('is_admin'))}
        pairs=set()
        items=[]
        for file_id in ids:
            file=source_files[file_id];relative=self._safe_name(str(file.get('name') or ''))
            try:size=int(file.get('size'))
            except (TypeError,ValueError):raise AppError('LIBRARY_SIZE_INVALID','文件大小未确认',409)
            if size<0:raise AppError('LIBRARY_SIZE_INVALID','文件大小未确认',409)
            if _is_video(str(relative)):pairs.update(_episodes(str(relative),season))
            item_id=secrets.token_hex(16);items.append((item_id,task_id,file_id,str(relative),size,str(root/relative),'QUEUED',''))
        episodes=sorted({episode for item_season,episode in pairs if season is None or item_season in {None,season}})
        missing=sorted(set(aired)-set(episodes))
        with self.store.transaction() as db:
            if not self._following_valid(db,owner,following):return None
            # Manual requests and RSS dispatch have separate workers. Recheck
            # under the write lock so both cannot create a job for one transfer.
            concurrent=db.execute('SELECT * FROM library_tasks WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1',(str(tid),)).fetchone()
            concurrent=dict(concurrent) if concurrent else None
            if not concurrent:
                self._reserve_targets(db,items)
                db.execute('INSERT INTO library_tasks (id,transfer_id,owner_user_id,status,root_path,metadata,expected_episodes,aired_episodes,episodes,missing_episodes,error,lease_until,lease_owner,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (task_id,str(tid),owner,'QUEUED',str(root),json.dumps(metadata,ensure_ascii=False),json.dumps(expected),json.dumps(aired),json.dumps(episodes),json.dumps(missing),'',0,None,now,now))
                db.executemany('INSERT INTO library_items VALUES(?,?,?,?,?,?,?,?)',[(x[0],x[1],x[2],x[3],x[4],x[5],x[6],x[7]) for x in items])
        if concurrent:return self._extend_existing(user,concurrent,source_files,ids,owner,following)
        return self.task(user,task_id)

    def request(self,user,tid,data=None):
        """Durable, explicit VPS intent; a cloud-only action never creates one."""
        data=data or {}
        source=self.workflow.get(user,tid)
        if data:
            if source['state']!='FILES_READY':raise AppError('FILES_NOT_READY','转存完成后可选择文件',409)
            return self.enqueue(user,tid,data)
        with self.store.transaction() as db:
            db.execute("INSERT INTO library_requests(transfer_id,user_id) VALUES(?,?) ON CONFLICT(transfer_id,user_id) DO UPDATE SET status='WAITING',error=NULL,next_retry=0",(tid,self._user_id(user)))
        self.enqueue_requests(tid)
        return self.task(user,tid)

    def enqueue_requests(self,tid=None):
        rows=self.store.all('''SELECT r.*,u.is_admin,t.state FROM library_requests r JOIN users u ON u.id=r.user_id
            JOIN transfers t ON t.id=r.transfer_id WHERE r.status='WAITING' AND r.next_retry<=? AND u.disabled=0'''+
            (' AND r.transfer_id=?' if tid else '')+' LIMIT 10',(time.time(),tid) if tid else (time.time(),))
        for row in rows:
            if row['state']!='FILES_READY':continue
            layout=getattr(self.workflow,'layout',None)
            if layout and not layout.ready(row['transfer_id']):continue
            naming=self.store.one('SELECT * FROM transfer_naming WHERE transfer_id=?',(row['transfer_id'],))
            if naming and naming['auto_rename'] and naming['status']!='DONE':continue
            with self.workflow.connection_lock:
                try:self.enqueue({'id':row['user_id'],'is_admin':row['is_admin']},row['transfer_id'])
                except AppError as error:
                    with self.store.transaction() as db:db.execute('UPDATE library_requests SET error=?,next_retry=? WHERE transfer_id=? AND user_id=?',(error.message,time.time()+60,row['transfer_id'],row['user_id']))
                else:
                    with self.store.transaction() as db:db.execute("UPDATE library_requests SET status='SUBMITTED',error=NULL WHERE transfer_id=? AND user_id=?",(row['transfer_id'],row['user_id']))

    def _catalog_episodes(self,mikan_id):
        """Use only an exact catalog work; no highest-number inference."""
        if not mikan_id or self.catalog is None:return None,[],[]
        try:work=self.catalog.work(mikan_id) or {}
        except Exception:return None,[],[]
        scope=self.catalog.tmdb_scope(work) if hasattr(self.catalog,'tmdb_scope') else None
        if scope and scope[0]=='tv' and scope[3] is not None:
            # An episode-level identity link does not mean the series has one
            # episode. Reuse the exact season verified by the naming pipeline.
            season_data=self.store.cache_get(f'cloud-naming-season:{scope[1]}:{scope[2]}',stale=True)
            if not season_data or str(season_data.get('tmdb_id'))!=str(scope[1]) or season_data.get('defaultSeason')!=scope[2]:return scope[2],[],[]
            work={**work,'episodes':season_data.get('episodes') or []}
        episodes=work.get('episodes') or []
        seasons=sorted({int(e.get('season_number')) for e in episodes if str(e.get('season_number','')).isdigit()})
        season=seasons[0] if len(seasons)==1 else None
        expected=sorted({int(e.get('episode_number')) for e in episodes
                         if str(e.get('episode_number','')).isdigit() and int(e.get('episode_number'))>0
                         and (season is None or int(e.get('season_number'))==season)})
        today=datetime.date.today().isoformat();aired=sorted({int(e.get('episode_number')) for e in episodes
                         if str(e.get('episode_number','')).isdigit() and int(e.get('episode_number'))>0
                         and (season is None or int(e.get('season_number'))==season)
                         and isinstance(e.get('air_date'),str) and e.get('air_date')[:10] <= today})
        return season,expected,aired

    def _claim(self,library_id=None):
        now=time.time();owner=secrets.token_hex(8)
        with self.store.transaction() as db:
            if library_id:
                row=db.execute('SELECT * FROM library_tasks WHERE id=?',(library_id,)).fetchone()
            else:
                row=db.execute("SELECT * FROM library_tasks WHERE (status='QUEUED' OR (status='DOWNLOADING' AND lease_until<?)) AND (lease_until<?) ORDER BY created_at LIMIT 1",(now,now)).fetchone()
            if not row:return None
            row=dict(row)
            if row['status'] not in {'QUEUED','DOWNLOADING'} or row['lease_until']>now:return None
            updated=db.execute("UPDATE library_tasks SET status='DOWNLOADING',lease_until=?,lease_owner=?,updated_at=? WHERE id=? AND (status='QUEUED' OR (lease_until<?))",(now+180,owner,now,row['id'],now))
            if updated.rowcount!=1:return None
        row['status']='DOWNLOADING';row['lease_owner']=owner;return row

    def _finish(self,task_id,status,error=''):
        with self.store.transaction() as db:
            db.execute('UPDATE library_tasks SET status=?,error=?,lease_until=0,lease_owner=NULL,updated_at=? WHERE id=?',(status,error,time.time(),task_id))

    def _download_item(self,row,item):
        target=Path(item['target_path']).resolve()
        if not target.is_relative_to(self.root):raise AppError('UNSAFE_LIBRARY_PATH','入库目录不安全',503)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if target.is_file() and target.stat().st_size==int(item['size']):return
            raise AppError('LIBRARY_FILE_CONFLICT','本地文件已存在且大小不一致',409)
        stream=None;temp=None;written=0;last_renew=time.time()
        try:
            owner=json.loads(row['metadata'])
            file_info,stream=self.workflow.download({'id':row['owner_user_id'],'is_admin':bool(owner.get('owner_is_admin'))},row['transfer_id'],item['file_id'])
            if str(file_info.get('id'))!=str(item['file_id']) or int(file_info.get('size'))!=int(item['size']):
                raise AppError('LIBRARY_FILE_CHANGED','任务文件已改变，未写入本地目录',409)
            if getattr(stream,'status',200) not in (200,206):raise AppError('LIBRARY_UPSTREAM_FAILED','Alist 未返回文件流',502)
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.parent/('.part-'+secrets.token_hex(12))
            with open(temp,'xb') as output:
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk:break
                    written+=len(chunk);output.write(chunk)
                    if time.time()-last_renew>=30:
                        with self.store.transaction() as db:db.execute('UPDATE library_tasks SET lease_until=?,updated_at=? WHERE id=? AND lease_owner=?',(time.time()+180,time.time(),row['id'],row['lease_owner']))
                        last_renew=time.time()
                output.flush();os.fsync(output.fileno())
            if written!=int(item['size']):raise AppError('LIBRARY_SIZE_MISMATCH','文件大小校验失败，未提交入库文件',409)
            try:os.link(temp,target)
            except FileExistsError:raise AppError('LIBRARY_FILE_CONFLICT','本地文件已存在且未覆盖',409)
            os.unlink(temp)
        except OSError:
            raise AppError('LIBRARY_WRITE_FAILED','本地文件写入失败，未提交入库文件',507)
        finally:
            if stream is not None:stream.close()
            if temp is not None and temp.exists():temp.unlink()

    def _process(self,row):
        library_id=row['id']
        try:
            items=self.store.all('SELECT * FROM library_items WHERE library_task_id=? ORDER BY id',(library_id,))
            for item in items:
                if item['status']=='COMPLETE':continue
                try:
                    self._download_item(row,item)
                    with self.store.transaction() as db:db.execute("UPDATE library_items SET status='COMPLETE',error='' WHERE id=?",(item['id'],))
                except AppError as error:
                    with self.store.transaction() as db:db.execute("UPDATE library_items SET status='FAILED',error=? WHERE id=?",(error.message,item['id']))
                    raise
            self._finish(library_id,'COMPLETE')
        except AppError as error:self._finish(library_id,'FAILED',error.message)
        except Exception:self._finish(library_id,'FAILED','入库任务处理失败，请重试')
        return self.task(None,library_id)

    def run(self,library_id):
        row=self._claim(library_id)
        if not row:return self.task(None,library_id)
        return self._process(row)

    def tick(self,limit=1):
        self.enqueue_requests()
        results=[]
        for _ in range(max(0,int(limit))):
            row=self._claim()
            if not row:break
            results.append(self._process(row))
        return results

    def task(self,user,tid):
        row=self._task_row(tid)
        if not row:
            row=self.store.one('SELECT * FROM library_tasks WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1',(tid,))
        if not row:
            source=self.workflow.get(user,tid)
            request=self.store.one("SELECT * FROM library_requests WHERE transfer_id=? AND user_id=? AND status='WAITING'",(tid,self._user_id(user)))
            if request:
                if source['state'] in {'FAILED','AUTH_REQUIRED','CANCELED'}:status='waiting_cloud'
                elif source['state']=='FILES_READY':status='waiting_organize'
                else:status='waiting_cloud'
                return {'available':False,'state':status,'status':status,'transfer_id':tid,'task_id':None,
                        'error':request['error'] or source.get('error_message') or (source.get('naming') or {}).get('error'),'files':[]}
            layout=getattr(self.workflow,'layout',None)
            ready=source.get('state')=='FILES_READY' and bool(source.get('files')) and (not layout or layout.ready(tid))
            return {'available':ready,'state':'IDLE','status':'idle','transfer_id':str(tid),'task_id':None,
                    'episodes':[],'missing_episodes':[],'expected_episodes':[],'episode_count':0,'task_ids':[],'release_hashes':[]}
        if user is not None:self._authorize(user,row)
        items=self.store.all('SELECT file_id,name,size,status,error FROM library_items WHERE library_task_id=? ORDER BY name',(row['id'],))
        metadata=json.loads(row['metadata']);expected=json.loads(row['expected_episodes']);episodes=json.loads(row['episodes'])
        return {'available':True,'task_id':row['id'],'transfer_id':row['transfer_id'],'status':row['status'].lower(),'state':row['status'].lower(),'error':row['error'],
                'metadata':metadata,'episodes':episodes,'missing_episodes':json.loads(row['missing_episodes']),
                'expected_episodes':expected,'episode_count':len(expected),'aired_episodes':json.loads(row['aired_episodes'] or '[]'),'files':items}

    def retry(self,user,library_id):
        row=self._authorize(user,self._task_row(library_id))
        if row['status']!='FAILED':raise AppError('LIBRARY_NOT_RETRYABLE','入库任务当前不需要重试',409)
        with self.store.transaction() as db:
            db.execute("UPDATE library_items SET status='QUEUED',error='' WHERE library_task_id=? AND status='FAILED'",(library_id,))
            db.execute("UPDATE library_tasks SET status='QUEUED',error='',lease_until=0,lease_owner=NULL,updated_at=? WHERE id=?",(time.time(),library_id))
        return self.task(user,library_id)

    def summary(self,user,ids):
        if not isinstance(ids,list):raise AppError('LIBRARY_IDS_INVALID','入库任务标识不正确',400)
        owner=self._user_id(user);requested=[str(value) for value in ids]
        rows=self.store.all('SELECT * FROM library_tasks ORDER BY created_at') if user.get('is_admin') else self.store.all('SELECT * FROM library_tasks WHERE owner_user_id=? ORDER BY created_at',(owner,))
        grouped={key:[] for key in requested}
        for row in rows:
            metadata=json.loads(row['metadata']);key=str(metadata.get('mikan_id') or '')
            if key in grouped:grouped[key].append(row)
        items={}
        for key in requested:
            matches=grouped[key];expected=sorted({e for row in matches for e in json.loads(row['expected_episodes'])})
            aired=sorted({e for row in matches for e in json.loads(row['aired_episodes'] or '[]')})
            episodes=set();hashes=set()
            for row in matches:
                metadata=json.loads(row['metadata']);hashes.update(x for x in [metadata.get('release_hash')] if x)
                for file in self.store.all('SELECT name,size,target_path,status FROM library_items WHERE library_task_id=?',(row['id'],)):
                    if file['status']!='COMPLETE' or not _is_video(file['name']):continue
                    path=Path(file['target_path'])
                    try:valid=path.is_file() and path.stat().st_size==int(file['size'])
                    except (OSError,TypeError,ValueError):valid=False
                    if valid:episodes.update(ep for _,ep in _episodes(file['name'],metadata.get('season')))
            episodes=sorted(episodes)
            statuses={row['status'] for row in matches}
            missing=sorted(set(aired)-set(episodes))
            if not matches or not episodes:status='not_in_library'
            elif expected and set(expected)<=set(episodes):status='complete'
            elif aired and set(aired)<=set(episodes):status='caught_up'
            else:status='partial'
            items[key]={'mikan_id':key,'status':status,'episodes':episodes,'missing_episodes':missing,
                        'expected_episodes':expected,'episode_count':len(expected),'aired_episodes':aired,'task_ids':[row['id'] for row in matches],
                        'release_hashes':sorted(hashes)}
        return {'items':items}
