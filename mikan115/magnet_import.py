"""Multiple-magnet preview, release grouping and shared acquisition admission."""
import hashlib
import html
import json
import re
import urllib.parse
from . import releases
from .errors import AppError
from .feed_matching import normalized
from .workflow import normalize_magnet
from .mikan_parser import parse_mikan_table_rows


def describe(ident,magnet,title):
    release=releases.parse(title)
    stem=re.sub(r'^\s*[\[【][^\]】]+[\]】]\s*','',title)
    boundary=re.search(r'(?i)\bS\d{1,2}[ ._-]*E\d|(?:\s-\s*|[\[【])\d{1,4}(?=v\d|[\]】\s_.(]|$)|第\s*\d+\s*[话話集]',stem)
    if boundary:stem=stem[:boundary.start()]
    stem=re.sub(r'[\[【][^\]】]*[\]】]','',stem).strip(' ._-')
    group_key=hashlib.sha256(json.dumps([normalized(stem),release['season'],normalized(release['group'])],ensure_ascii=False).encode()).hexdigest()[:24]
    return {'infohash':ident,'magnet':magnet,'title':title or ident,'named':bool(title),'release':release,'group_key':group_key,
        'series_label':stem or '未命名资源','aliases':[title] if title else [],'label_conflict':False}


def extract(text):
    if not isinstance(text,str) or len(text)>60000:raise AppError('INVALID_MAGNETS','一次最多读取 100 个磁力链接')
    links=re.findall(r'magnet:\?[^\s<>"\']*?(?=magnet:\?|[\s<>"\']|$)',html.unescape(text),re.I)
    if not links:raise AppError('MAGNET_REQUIRED','请粘贴磁力链接')
    if len(links)>100:raise AppError('TOO_MANY_MAGNETS','一次最多读取 100 个磁力链接')
    items={};invalid=[];duplicates=0
    for index,link in enumerate(links,1):
        try:
            ident,magnet=normalize_magnet(link.rstrip(',;，；'))
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
            title=(query.get('dn') or [''])[0];release=releases.parse(title)
            if ident in items:
                duplicates+=1;old=items[ident]
                if title and not old['named']:
                    items[ident]=describe(ident,magnet,title);continue
                if title and old['named'] and title!=old['title']:
                    old['aliases']=list(dict.fromkeys([*old['aliases'],title]))
                    if any(release[key]!=old['release'][key] for key in ('group','season','episodes')):old['label_conflict']=True
                continue
            items[ident]=describe(ident,magnet,title)
        except AppError as error:invalid.append({'index':index,'reason':error.message})
    return list(items.values()),invalid,duplicates


class MagnetImport:
    def __init__(self,workflow,inventory,library,catalog):self.workflow,self.inventory,self.library,self.catalog=workflow,inventory,library,catalog

    def labels(self,user,items):
        """Use exact-hash local records when dn is absent; never infer work IDs."""
        result=[];pages={}
        for item in items:
            if item['named'] or item['label_conflict']:result.append(item);continue
            previous=self.workflow.store.one('''SELECT t.title FROM transfers t WHERE t.infohash=? AND
                (EXISTS(SELECT 1 FROM transfer_owners o WHERE o.transfer_id=t.id AND o.user_id=?) OR ?=1) LIMIT 1''',(item['infohash'],user['id'],int(user.get('is_admin',False))))
            titles=set()
            if previous and previous['title']!=item['infohash']:titles.add(previous['title'])
            for source in self.workflow.store.all('SELECT DISTINCT bangumi_id FROM release_sources WHERE resource_hash=?',(item['infohash'],)):
                bid=source['bangumi_id']
                if bid not in pages:
                    page=self.workflow.store.cache_get('mikan:/Home/Bangumi/'+bid,stale=True)
                    pages[bid]=parse_mikan_table_rows(page) if isinstance(page,str) else []
                for resource in pages[bid]:
                    if resource.get('resourceHash')!=item['infohash'] or not resource.get('magnetUrl'):continue
                    try:ident,_=normalize_magnet(resource['magnetUrl'])
                    except AppError:continue
                    if ident==item['infohash']:titles.add(resource['title'])
            result.append(describe(item['infohash'],item['magnet'],next(iter(titles))) if len(titles)==1 else item)
        return result

    def plan(self,user,body):
        if set(body)-{'text','selected','bindings','destination','digest'}:raise AppError('INVALID_MAGNETS','导入参数无效')
        destination=body.get('destination','cloud')
        if destination not in {'cloud','vps'}:raise AppError('INVALID_DESTINATION','请选择保存位置')
        items,invalid,duplicates=extract(body.get('text',''));items=self.labels(user,items)
        bindings=body.get('bindings') or {};selected=body.get('selected')
        if not isinstance(bindings,dict) or len(bindings)>100 or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>150 for k,v in bindings.items()):raise AppError('INVALID_MAGNETS','作品关联无效')
        keys={item['infohash'] for item in items};groups={item['group_key'] for item in items}
        if set(bindings)-groups:raise AppError('INVALID_MAGNETS','资源分组已变化')
        if selected is not None and (not isinstance(selected,list) or len(selected)>100 or any(not isinstance(key,str) or key not in keys for key in selected)):
            raise AppError('INVALID_MAGNETS','请选择有效的磁力链接')
        snapshots={};ready=[];public=[];binding=self.workflow.store.binding(False)
        for item in items:
            work_id=bindings.get(item['group_key']);context=None;snapshot=None;reason='';release=item['release'];rule=releases.rules()
            if work_id:
                if work_id not in snapshots:snapshots[work_id]=self.inventory.snapshot(user,work_id,refresh='digest' in body)
                context,snapshot=snapshots[work_id];rule['season']=context['season']
                if release['season'] is not None and context['season'] is not None and release['season']!=context['season']:reason='季度不符'
                episodes=releases.normalized_episodes(release,rule)
                if not reason and episodes and set(episodes)&self.inventory.owned(snapshot,destination):reason='已有剧集'
                if not reason and episodes and set(episodes)&set(snapshot['pending'][destination]):reason='正在处理'
                old=self.workflow.store.one('SELECT t.work_id,n.metadata FROM transfers t LEFT JOIN transfer_naming n ON n.transfer_id=t.id WHERE t.account_uid=? AND t.infohash=?',
                    (binding['account_uid'] if binding else '',item['infohash']))
                if old:
                    old_scope=json.loads(old.get('metadata') or '{}').get('scope_key')
                    if not old_scope and old['work_id']:old_scope=self.inventory.context(old['work_id'])['scope_key']
                    if old_scope and old_scope!=context['scope_key']:reason='相同磁力已关联其他作品'
            if item['label_conflict']:reason=reason or '同一磁力的名称冲突'
            chosen=selected is None or item['infohash'] in selected
            if chosen and not reason:ready.append((item,context,snapshot,rule))
            public.append({k:v for k,v in item.items() if k not in {'magnet','aliases'}}|{'reason':reason,'selected':chosen and not reason,'work_id':work_id or ''})
        fingerprints=[(key,value[1]['digest']) for key,value in sorted(snapshots.items())]
        digest=hashlib.sha256(json.dumps([user['id'],body['text'],[(i['infohash'],i['magnet']) for i in items],destination,bindings,sorted(selected) if selected is not None else None,fingerprints],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if 'digest' in body:
            if digest!=body['digest']:raise AppError('MAGNET_PLAN_CHANGED','库存或选择已变化，请重新读取',409)
            self.workflow.store.binding()
            if any(not value[1]['cloud_verified'] for value in snapshots.values()):raise AppError('INVENTORY_UNAVAILABLE','库存未确认，请重试',409)
            tasks={};downloads=[];failed=[]
            for item,context,snapshot,rule in ready:
                metadata=None;magnet=item['magnet'];reuse=None
                if context:
                    episodes=releases.normalized_episodes(item['release'],rule)
                    metadata={'work_id':context['work_id'],'bangumi_id':context['mikan_id'],'scope_key':context['scope_key'],'rules':rule,'release':item['release'],
                        'acquisition':{'scope_key':context['scope_key'],'episodes':episodes,'destination':destination,'dedupe':'episode',
                            'verified_tasks':list({f['transfer_id'] for f in snapshot['cloud']}|set(snapshot['pending_tasks']))}}
                    if destination=='vps' and episodes:reuse=self.inventory.reusable(snapshot,episodes,rule)
                    if reuse:magnet=self.workflow.authorize(user,reuse['transfer_id'])['magnet']
                try:
                    task=self.workflow.create(user,magnet,item['title'],context['work_id'] if context else '',enqueue_only=True,naming=metadata)
                    tasks[task['id']]=task
                    if destination=='vps':
                        selection={'file_ids':[file['file_id'] for file in snapshot['cloud'] if file['transfer_id']==task['id'] and set(episodes)&set(file['episodes'])]} if reuse else {}
                        downloads.append(self.library.request(user,task['id'],selection))
                except AppError as error:failed.append({'infohash':item['infohash'],'reason':error.message})
            return {'success':not failed,'tasks':list(tasks.values()),'downloads':downloads,'merged':max(0,len(ready)-len(tasks)-len(failed)),'skipped':len(items)-len(ready),'invalid':invalid,'failed':failed}
        return {'items':public,'invalid':invalid,'duplicates':duplicates,'digest':digest,'selected_count':len(ready),
            'connection_ready':bool(binding and binding['ready']),'destination':destination}
