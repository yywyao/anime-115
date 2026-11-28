"""Destination-specific inventory from verified managed files, never release counts."""
import hashlib
import json
import posixpath
import time
import datetime
from pathlib import Path

from . import releases
from .errors import AppError
from .library import _episodes, _is_video


def work_scope(catalog,work):
    scope=catalog.tmdb_scope(work)
    if scope:
        kind,ident,season,_=scope
        if kind=='movie':return 'tmdb:movie:'+str(ident)
        return f'tmdb:tv:{ident}:s{season}'
    bid=str(work.get('mikan_id') or work.get('bangumiId') or '')
    return 'mikan:'+bid if bid.isdigit() else ''


class Inventory:
    def __init__(self,store,catalog,workflow,library):
        self.store,self.catalog,self.workflow,self.library=store,catalog,workflow,library

    def context(self,ident):
        work=self.catalog.work(str(ident));scope=self.catalog.tmdb_scope(work)
        bid=str(work.get('mikan_id') or work.get('bangumiId') or (str(ident) if str(ident).isdigit() else ''))
        key=work_scope(self.catalog,work)
        if not key:raise AppError('WORK_REQUIRED','请先选择作品',409)
        return {'work':work,'scope_key':key,'season':scope[2] if scope and scope[0]=='tv' else None,
                'work_id':str(work.get('canonical_id') or work.get('id') or ident),'mikan_id':bid,
                'title':work.get('series_title') or work.get('homeTitle') or work.get('title') or ''}

    def _tasks(self,user,context):
        rows=self.store.all('''SELECT t.*,n.metadata,n.auto_rename,n.status naming_status FROM transfers t
            LEFT JOIN transfer_naming n ON n.transfer_id=t.id WHERE '''+
            ('1=1' if user.get('is_admin') else 'EXISTS(SELECT 1 FROM transfer_owners o WHERE o.transfer_id=t.id AND o.user_id=?)'),
            () if user.get('is_admin') else (user['id'],))
        result=[];scopes={context['work_id']:context['scope_key'],context['mikan_id']:context['scope_key']}
        for row in rows:
            meta=json.loads(row.get('metadata') or '{}');ident=str(meta.get('work_id') or row.get('work_id') or '')
            if not ident:continue
            key=meta.get('scope_key')
            if not key:
                if ident not in scopes:
                    try:scopes[ident]=work_scope(self.catalog,self.catalog.work(ident))
                    except AppError:scopes[ident]=''
                key=scopes.get(ident)
            if key==context['scope_key']:result.append((row,meta))
        return result

    @staticmethod
    def _release(file,meta):
        parsed=releases.parse(posixpath.basename(file['name']));original=meta.get('release') or {}
        for field in ('group','group_id','version','resolution','subtitle_mode'):
            if parsed.get(field) in (None,'','unknown'):parsed[field]=original.get(field,parsed.get(field))
        return parsed

    def snapshot(self,user,ident,*,refresh=False):
        context=self.context(ident);tasks=self._tasks(user,context);binding=self.store.binding(False)
        account=str(binding['account_uid']) if binding else 'unbound'
        cache_key=f'inventory:v1:{user["id"]}:{account}:{context["scope_key"]}'
        signature=hashlib.sha256(json.dumps([(t['id'],t['state'],t['updated_at']) for t,_ in tasks],sort_keys=True).encode()).hexdigest()
        cached=self.store.cache_get(cache_key,stale=True) or {}
        cloud=[];error=None;verified=False
        if not refresh and cached.get('signature')==signature and cached.get('verified_at',0)>time.time()-30:
            cloud=cached.get('files',[]);verified=True
        elif binding and binding['ready']:
            try:
                with self.workflow.connection_lock:
                    b,pan=self.workflow.scope();folders={}
                    for task,meta in tasks:
                        if task['account_uid']!=account or task['state']!='FILES_READY':continue
                        layout=self.workflow.layout.row(task['id']) if self.workflow.layout else None
                        if not layout or layout['status']!='PUBLISHED':continue
                        cid=layout['series_cid']
                        if cid not in folders:
                            pan.verify_child(b['root_cid'],cid,layout['series_name'])
                            folders[cid]={str(r.get('fid')):r for r in pan.list_files(cid) if r.get('fid')}
                        files=self.store.all('''SELECT f.*,o.remote_id,o.new_path FROM transfer_files f
                            JOIN cloud_outputs o ON o.file_id=f.id AND o.transfer_id=f.transfer_id
                            WHERE f.transfer_id=? AND o.status='DONE' ''',(task['id'],))
                        for file in files:
                            if not _is_video(file['name']):continue
                            remote=folders[cid].get(file['remote_id'])
                            if not remote or int(remote.get('s',-1))!=file['size'] or str(remote.get('n') or remote.get('name'))!=posixpath.basename(file['name']):continue
                            eps=sorted(ep for season,ep in _episodes(file['name'],context['season']) if season in {None,context['season']})
                            if not eps:continue
                            cloud.append({'transfer_id':task['id'],'file_id':file['id'],'remote_id':file['remote_id'],
                                          'infohash':task['infohash'],'resource_hash':meta.get('resource_hash',''),'episodes':eps,'release':self._release(file,meta),'title':task['title']})
                    verified=True
                self.store.cache_set(cache_key,{'files':cloud,'signature':signature,'verified_at':time.time()},60)
            except AppError as failure:
                error=failure.message;cloud=cached.get('files',[])
        else:
            error='请先连接 115';cloud=cached.get('files',[])
        local=[];pending_cloud=set();pending_vps=set();pending_tasks=[];task_map={t['id']:(t,m) for t,m in tasks}
        for task,meta in tasks:
            rule=releases.rules(meta.get('rules') or {})
            release=meta.get('release') or releases.parse(task['title'])
            eps={e for e in releases.normalized_episodes(release,rule) if 0<e<=9999}
            if task['state'] not in {'FILES_READY','FAILED','CANCELED'}:pending_cloud.update(eps)
            elif task['state']=='FILES_READY' and not any(f['transfer_id']==task['id'] for f in cloud):
                layout=self.workflow.layout.row(task['id']) if self.workflow.layout else None
                if not layout or layout['status']!='PUBLISHED':pending_cloud.update(eps);pending_tasks.append(task['id'])
            requests=self.store.one("SELECT 1 FROM library_requests WHERE transfer_id=? AND user_id=? AND status='WAITING'",(task['id'],user['id']))
            if requests:pending_vps.update(eps)
        for row in self.store.all('SELECT * FROM library_tasks'):
            pair=task_map.get(row['transfer_id'])
            if not pair or (not user.get('is_admin') and row['owner_user_id']!=user['id']):continue
            task,meta=pair
            for file in self.store.all('SELECT * FROM library_items WHERE library_task_id=?',(row['id'],)):
                if not _is_video(file['name']):continue
                eps=sorted(e for s,e in _episodes(file['name'],context['season']) if s in {None,context['season']})
                if file['status']=='COMPLETE':
                    path=Path(file['target_path'])
                    try:exists=path.resolve().is_relative_to(self.library.root.resolve()) and not path.is_symlink() and path.is_file() and path.stat().st_size==file['size']
                    except OSError:exists=False
                    if exists and eps:local.append({'transfer_id':task['id'],'file_id':file['file_id'],'infohash':task['infohash'],
                                                   'resource_hash':meta.get('resource_hash',''),'episodes':eps,'release':self._release(file,meta),'title':task['title']})
                elif row['status'] in {'QUEUED','DOWNLOADING'}:pending_vps.update(eps)
        _,expected,aired=self.library._catalog_episodes(context['mikan_id']) if context['mikan_id'].isdigit() else (None,[],[])
        if not expected:
            selected=context['season']
            episodes=context['work'].get('episodes') or []
            expected=sorted({e['episode_number'] for e in episodes if type(e.get('episode_number')) is int and e['episode_number']>0 and e.get('season_number')==selected})
            aired=sorted({e['episode_number'] for e in episodes if e.get('episode_number') in expected and e.get('season_number')==selected and
                isinstance(e.get('air_date'),str) and len(e['air_date'])>=10 and e['air_date'][:10]<=datetime.date.today().isoformat()})
        data={'cloud':cloud,'vps':local,'pending':{'cloud':sorted(pending_cloud),'vps':sorted(pending_vps)},
              'pending_tasks':pending_tasks,
              'expected_episodes':expected,'aired_episodes':aired,'cloud_verified':verified,'error':error,'scope_key':context['scope_key']}
        data['digest']=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return context,data

    @staticmethod
    def owned(snapshot,destination):return {ep for file in snapshot[destination] for ep in file['episodes']}

    @staticmethod
    def reusable(snapshot,episodes,rule):
        for file in snapshot['cloud']:
            if set(episodes)<=set(file['episodes']) and not releases.admit({'title':file['title'],'release':file['release']},{**rule,'allow_batches':True}):return file
        return None
