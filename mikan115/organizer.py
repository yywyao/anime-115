"""Preview and scoped 115 file renaming, with durable reconciliation."""
import hashlib,json,posixpath,re,time,unicodedata,urllib.parse
from .errors import AppError
from . import releases

MEDIA={'.mkv','.mp4','.avi','.m4v','.ts','.webm','.mov'}
SUBS={'.ass','.ssa','.srt','.sub','.idx','.vtt'}
def clean(text,limit=100):
    text=unicodedata.normalize('NFKC',str(text));text=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',text).strip(' .')
    return text[:limit].strip(' .') or '未命名'

class Organizer:
    def __init__(self,store,workflow,catalog,sonarr):self.store=store;self.workflow=workflow;self.catalog=catalog;self.sonarr=sonarr
    def authorize(self,user,tid):
        task=self.workflow.authorize(user,tid)
        if not user['is_admin'] and self.store.one('SELECT count(*) n FROM transfer_owners WHERE transfer_id=?',(tid,))['n']>1:raise AppError('SHARED_TASK','共享任务请由管理员改名',403)
        if task['state']!='FILES_READY':raise AppError('FILES_NOT_READY','转存完成后可改名',409)
        return task
    def preview(self,user,tid,override=None):
        task=self.authorize(user,tid);naming=self.store.one('SELECT * FROM transfer_naming WHERE transfer_id=?',(tid,));meta=json.loads(naming['metadata']) if naming else {}
        work=self.catalog.work(meta.get('work_id') or task['work_id']) if (meta.get('work_id') or task['work_id']) else {}
        if not work.get('title'):raise AppError('WORK_REQUIRED','请先关联作品',409)
        if override and (not isinstance(override,dict) or set(override)-{'season','episode_offset','episode_title'}):raise AppError('INVALID_RULES','改名规则无效')
        rule=releases.rules({**(meta.get('rules') or {}),**(override or {})});release=meta.get('release') or releases.parse(task['title'])
        rows=self.store.all('SELECT * FROM transfer_files WHERE transfer_id=? ORDER BY path',(tid,));media=[f for f in rows if posixpath.splitext(f['name'])[1].casefold() in MEDIA]
        plans=[];skipped=[];bases={}
        for f in media:
            name=posixpath.basename(f['name']);parsed=releases.parse(name)
            if re.search(r'(?i)(?:\b(?:sample|trailer|ncop|nced|op|ed)\b|特典|预告|預告)',name):skipped.append({'name':f['name'],'reason':'附加内容'});continue
            episodes=releases.normalized_episodes(parsed,rule)
            release_episodes=releases.normalized_episodes(release,rule)
            if len(media)==1 and parsed['episodes'] and len(release_episodes)==1:
                if parsed['episodes']==release_episodes:episodes=parsed['episodes']
                elif episodes!=release_episodes:skipped.append({'name':f['name'],'reason':'文件与资源集号不一致'});continue
            if not parsed['episodes'] and len(media)==1 and not release.get('batch'):episodes=release_episodes
            if len(episodes)!=1:skipped.append({'name':f['name'],'reason':'集号未确认'});continue
            season=rule['season'] if rule['season'] is not None else work.get('tmdb_season') if (work.get('tmdb_mapping') or {}).get('status')=='linked' else parsed['season']
            if season is None:skipped.append({'name':f['name'],'reason':'请在追新规则中指定季号'});continue
            if parsed['season'] is not None and parsed['season']!=season:skipped.append({'name':f['name'],'reason':'季号不一致'});continue
            ep=episodes[0]
            if not 0<=ep<=9999:skipped.append({'name':f['name'],'reason':'集号偏移无效'});continue
            self.sonarr.verify(f'{work.get("title_original") or work["title"]} S{season:02d}E{ep:02d}',work,season,ep)
            tag=release if release.get('version')!='unknown' else parsed
            tokens=[release.get('group') or parsed.get('group'),releases.VERSIONS.get(tag.get('version')) if tag.get('version') not in (None,'unknown') else '',tag.get('resolution') if tag.get('resolution') not in (None,'unknown') else '']
            base=clean(work['title'])+f' - S{season:02d}E{ep:02d}'
            if rule['episode_title']:
                titles=[e.get('title') for e in work.get('episodes',[]) if e.get('season_number')==season and e.get('episode_number')==ep and e.get('title')]
                if len(titles)==1:base+=' - '+clean(titles[0],60)
            base+=''.join(' ['+clean(v,40)+']' for v in tokens if v)
            new=posixpath.join(posixpath.dirname(f['path']),base+posixpath.splitext(name)[1].lower())
            bases[posixpath.splitext(f['path'])[0]]=posixpath.splitext(new)[0]
            if new!=f['path']:plans.append({'file_id':f['id'],'old_path':f['path'],'new_path':new,'old_name':f['name'],'new_name':new[len(task['task_path'])+1:],'size':f['size']})
        for f in rows:
            stem,ext=posixpath.splitext(f['path'])
            if ext.casefold() not in SUBS:continue
            matches=[(old,new) for old,new in bases.items() if stem==old or (stem.startswith(old+'.') and '/' not in stem[len(old):])]
            if len(matches)!=1:continue
            old,new=matches[0];target=new+stem[len(old):]+ext
            if target!=f['path']:plans.append({'file_id':f['id'],'old_path':f['path'],'new_path':target,'old_name':f['name'],'new_name':target[len(task['task_path'])+1:],'size':f['size']})
        targets=set();existing={unicodedata.normalize('NFKC',f['path']).casefold():f['id'] for f in rows}
        for p in plans:
            key=unicodedata.normalize('NFKC',p['new_path']).casefold()
            if key in targets or (key in existing and existing[key]!=p['file_id']):raise AppError('RENAME_COLLISION','目标文件名重复，已保留原名',409)
            targets.add(key)
        digest=hashlib.sha256(json.dumps(plans,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'files':[{k:v for k,v in p.items() if k not in {'old_path','new_path'}} for p in plans],'skipped':skipped,'digest':digest,'_plans':plans}
    @staticmethod
    def public(plan):return {k:v for k,v in plan.items() if not k.startswith('_')}
    def apply(self,user,tid,digest,override=None):
        with self.workflow.connection_lock:
            task=self.authorize(user,tid)
            # Reconcile interrupted operations before making a fresh preview.
            pending=self.store.all("SELECT * FROM rename_operations WHERE transfer_id=? AND status!='DONE'",(tid,))
            for op in pending:self.execute(task,op)
            plan=self.preview(user,tid,override)
            if digest!=plan['digest'] and not (pending and not plan['files']):raise AppError('RENAME_PLAN_CHANGED','文件或规则已变化，请重新预览',409)
            for p in plan['_plans']:
                self.workflow.scope(task)
                relative=p['old_path'][len(task['task_path'])+1:]
                _,pan=self.workflow.scope(task);parent,item=pan.locate_file(task['task_cid'],relative)
                remote_id=str(item['fid'])
                with self.store.transaction() as db:db.execute('INSERT OR REPLACE INTO rename_operations VALUES(?,?,?,?,?,?,?,?,?)',(tid,p['file_id'],p['old_path'],p['new_path'],p['size'],'PREPARED',time.time(),remote_id,parent))
                self.execute(task,{**p,'remote_id':remote_id,'parent_cid':parent,'transfer_id':tid})
            with self.store.transaction() as db:db.execute("UPDATE transfer_naming SET status='DONE',error=NULL,updated_at=? WHERE transfer_id=?",(time.time(),tid))
            return {'renamed':len(plan['_plans'])+len(pending),'task':self.workflow.get(user,tid)}
    def execute(self,task,op):
        old,new=op['old_path'],op['new_path']
        for path in [old,new]:
            decoded=urllib.parse.unquote(path)
            if not path.startswith(task['task_path']+'/') or '..' in decoded.split('/') or '\\' in decoded or '\0' in decoded:raise AppError('UNSAFE_PATH','文件超出专用任务目录',403)
        if posixpath.dirname(old)!=posixpath.dirname(new):raise AppError('UNSAFE_PATH','改名不能移动文件',403)
        _,pan=self.workflow.scope(task);parent,rows=pan.file_parent(task['task_cid'],old[len(task['task_path'])+1:])
        if str(parent)!=str(op['parent_cid']):raise AppError('RENAME_SCOPE_CHANGED','文件目录已变化',409)
        names={str(r.get('n') or r.get('name')):r for r in rows};old_name=posixpath.basename(old);new_name=posixpath.basename(new)
        source=next((r for r in rows if str(r.get('fid'))==str(op['remote_id'])),None)
        if not source or int(source.get('s',source.get('size',-1)))!=op['size']:raise AppError('RENAME_SOURCE_CHANGED','文件已变化，请重新核对',409)
        current=str(source.get('n') or source.get('name'))
        collision=[r for r in rows if unicodedata.normalize('NFKC',str(r.get('n') or r.get('name'))).casefold()==unicodedata.normalize('NFKC',new_name).casefold() and str(r.get('fid'))!=str(op['remote_id'])]
        if collision:raise AppError('RENAME_COLLISION','目标文件已存在，未覆盖',409)
        if current==old_name:
            pan.rename_file(op['remote_id'],new_name)
            rows=pan.list_files(parent);source=next((r for r in rows if str(r.get('fid'))==str(op['remote_id'])),None)
            if not source or str(source.get('n') or source.get('name'))!=new_name:raise AppError('RENAME_PENDING','改名结果待确认，请重试核对',409)
        elif current!=new_name:raise AppError('RENAME_SOURCE_CHANGED','原文件名已变化，已停止改名',409)
        with self.store.transaction() as db:
            db.execute('UPDATE transfer_files SET path=?,name=? WHERE id=? AND transfer_id=?',(new,new[len(task['task_path'])+1:],op['file_id'],task['id']))
            db.execute("UPDATE rename_operations SET status='DONE',updated_at=? WHERE transfer_id=? AND file_id=? AND old_path=?",(time.time(),task['id'],op['file_id'],old))
        self.workflow.alist.list_files(posixpath.dirname(new))
        self.store.event(task['id'],'FILE_RENAMED','文件已改名')
    def tick(self):
        rows=self.store.all("SELECT n.* FROM transfer_naming n JOIN transfers t ON t.id=n.transfer_id WHERE n.auto_rename=1 AND n.status='PENDING' AND t.state='FILES_READY' LIMIT 5")
        for row in rows:
            owner=self.store.one('SELECT u.* FROM users u JOIN transfer_owners o ON o.user_id=u.id WHERE o.transfer_id=? AND u.disabled=0 ORDER BY u.id LIMIT 1',(row['transfer_id'],))
            if not owner:continue
            try:
                if self.store.one('SELECT count(*) n FROM transfer_owners WHERE transfer_id=?',(row['transfer_id'],))['n']>1:raise AppError('SHARED_TASK','共享任务需管理员手动改名',409)
                plan=self.preview(owner,row['transfer_id'])
                if not plan['files']:raise AppError('NAME_UNCONFIRMED','未取得可确认的新文件名，请手动核对',409)
                self.apply(owner,row['transfer_id'],plan['digest'])
            except AppError as e:
                with self.store.transaction() as db:db.execute("UPDATE transfer_naming SET status='REVIEW',error=?,updated_at=? WHERE transfer_id=?",(e.message,time.time(),row['transfer_id']))
