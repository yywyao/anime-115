"""Select and queue Mikan releases using exact source IDs."""
import hashlib
import json
import re
import urllib.parse

from . import releases
from .errors import AppError
from .torrent import torrent_magnet
from .workflow import normalize_magnet


class Batch:
    def __init__(self, catalog, workflow, library,inventory=None):
        self.catalog = catalog
        self.workflow = workflow
        self.library = library
        self.inventory=inventory

    @staticmethod
    def source_id(value):
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch('[0-9]{1,12}', str(value)):
            raise AppError('INVALID_SOURCE', '请选择蜜柑作品')
        return str(value)

    def candidates(self, bid):
        result = self.catalog.resources(bid, bid)
        for group in result.get('groups', []):
            for resource in group.get('resources', []):
                key = str(resource.get('resourceHash') or '').lower()
                if re.fullmatch('[a-f0-9]{40}', key):
                    yield {'key': key, 'title': resource['title'], 'resource': resource,
                           'release': releases.parse(resource['title'], group['subgroupId'], group['subgroupName'])}

    def magnet(self, item):
        resource = item['resource']
        if resource.get('magnetUrl'):
            return normalize_magnet(resource['magnetUrl'])
        source = urllib.parse.urlsplit(resource.get('torrentUrl') or '')
        if not re.fullmatch(r'/Download/[^/?#]+(?:/[^/?#]+)*\.torrent', source.path):
            raise AppError('MAGNET_UNAVAILABLE', '资源缺少磁力链接', 409)
        return normalize_magnet(torrent_magnet(self.catalog.fetch(source.path, binary=True), item['title']))

    def queue(self, user, bid, items, rule,*,destination='cloud',action='complete',context=None,snapshot=None):
        work=self.catalog.work(bid)
        scope=self.catalog.tmdb_scope(work)
        if scope and scope[0]=='tv' and scope[2] is not None:
            if rule['season'] not in (None,scope[2]):raise AppError('LIBRARY_SEASON_MISMATCH','季号与当前作品不一致',409)
            rule={**rule,'season':scope[2]}
        if self.inventory and context is None:context,snapshot=self.inventory.snapshot(user,bid)
        prepared = []
        for item in items:
            reusable=self.inventory.reusable(snapshot,releases.normalized_episodes(item['release'],rule),rule) if self.inventory and destination=='vps' and action!='upgrade' else None
            if reusable:
                existing=self.workflow.authorize(user,reusable['transfer_id'])
                prepared.append((existing['magnet'],{**item,'reusable':reusable}));continue
            resolved = dict(self.magnet(source) for source in item['sources'])
            if len(resolved) != 1:
                raise AppError('RESOURCE_CONFLICT', '资源来源不一致，请重新选择', 409)
            prepared.append((next(iter(resolved.values())), item))
        tasks = {}
        downloads=[]
        with self.workflow.connection_lock:
            for magnet, item in prepared:
                release=dict(item['release'])
                sources=[{'group_id':source['release']['group_id'],'group':source['release']['group']}
                         for source in item['sources']]
                if len({source['group_id'] for source in sources})>1 and not rule['group_ids']:
                    release.update(group_id='',group='')
                metadata = {'release':release,'sources':sources,'rules':rule,'work_id':bid,
                            'bangumi_id':bid,'resource_hash':item['key']}
                if context:
                    metadata['scope_key']=context['scope_key'];metadata['action']=action
                    metadata['acquisition']={'scope_key':context['scope_key'],'episodes':releases.normalized_episodes(release,rule),
                        'destination':destination,'dedupe':rule['dedupe'],'version':release.get('version','unknown'),
                        'upgrade':action=='upgrade','verified_tasks':list({f['transfer_id'] for f in (snapshot or {}).get('cloud',[])}|set((snapshot or {}).get('pending_tasks',[])))}
                reusable=item.get('reusable')
                if reusable:
                    existing=self.workflow.authorize(user,reusable['transfer_id']);magnet=existing['magnet']
                task = self.workflow.create(user, magnet, item['title'], bid, enqueue_only=True, naming=metadata)
                tasks[task['id']] = task
                if destination=='vps':
                    if reusable:
                        eps=set(releases.normalized_episodes(release,rule))
                        ids=[f['file_id'] for f in snapshot['cloud'] if f['transfer_id']==task['id'] and eps.intersection(f['episodes'])]
                        downloads.append(self.library.request(user,task['id'],{'file_ids':ids}))
                    else:downloads.append(self.library.request(user,task['id']))
        return {'success': True, 'tasks': list(tasks.values()),'downloads':downloads,'destination':destination}

    @staticmethod
    def grouped(candidates):
        grouped = {}
        for item in candidates:
            if item['key'] in grouped:
                grouped[item['key']]['sources'].append(item)
                continue
            grouped[item['key']] = {**item, 'sources': [item]}
        return grouped

    @staticmethod
    def release_ids(keys):
        if not isinstance(keys, list) or not 1 <= len(keys) <= 500 or any(
                not isinstance(key, str) or not re.fullmatch('[a-f0-9]{40}', key) for key in keys):
            raise AppError('INVALID_RELEASES', '请选择要转存的资源')
        return list(dict.fromkeys(keys))

    def selected(self, user, bid, keys, rules=None,*,destination='cloud'):
        bid = self.source_id(bid)
        keys=self.release_ids(keys)
        rule=releases.rules(rules)
        candidates = self.grouped(self.candidates(bid))
        chosen = []
        for key in keys:
            item = candidates.get(key)
            if not item:
                raise AppError('RESOURCE_NOT_FOUND', '资源已变化，请重新选择', 409)
            admitted=[source for source in item['sources'] if not releases.admit(source,{**rule,'allow_batches':True})]
            if not admitted:raise AppError('RESOURCE_FILTER_MISMATCH','所选资源不符合当前筛选，请重新选择',409)
            preferred=min(admitted,key=lambda source:releases.priority(source,rule))
            chosen.append({**preferred,'sources':item['sources']})
        return self.queue(user, bid, chosen, rule,destination=destination)

    def action(self, user, body):
        if set(body)-{'mikan_id','action','rules','digest','release_ids','destination','refresh'}:
            raise AppError('INVALID_LIBRARY_ACTION','操作参数无效')
        bid=self.source_id(body.get('mikan_id'));action=body.get('action')
        destination=body.get('destination','cloud')
        if action not in {'missing','complete','upgrade'}:raise AppError('INVALID_LIBRARY_ACTION','请选择收集方式')
        if destination not in {'cloud','vps'}:raise AppError('INVALID_DESTINATION','请选择保存位置')
        rule=releases.rules(body.get('rules'));rule['allow_batches']=True
        if action=='upgrade' and not any(rule[k] for k in ('group_ids','versions','resolutions')):
            raise AppError('UPGRADE_PREFERENCE_REQUIRED','请选择字幕组、分辨率或字幕')
        selected=(set(self.release_ids(body['release_ids'])) if body['release_ids'] else set()) if 'release_ids' in body else None
        context,snapshot=self.inventory.snapshot(user,bid,refresh=body.get('refresh') is True or 'digest' in body)
        if context['season'] is not None:
            if rule['season'] not in (None,context['season']):raise AppError('LIBRARY_SEASON_MISMATCH','季号与当前作品不一致',409)
            rule['season']=context['season']
        available=list(self.candidates(bid));all_keys={item['key'] for item in available}
        if selected and not selected<=all_keys:raise AppError('RESOURCE_NOT_FOUND','资源已变化，请刷新',409)
        owned=self.inventory.owned(snapshot,destination);pending=set(snapshot['pending'][destination])
        known=set(snapshot['aired_episodes']) if snapshot['expected_episodes'] else {e for item in available for e in releases.normalized_episodes(item['release'],rule) if 0<e<=9999}
        targets=(known-owned-pending) if action=='missing' else owned if action=='upgrade' else None
        owned_hashes={value for file in snapshot[destination] for value in (file['infohash'],file.get('resource_hash')) if value}
        eligible=[];groups={};excluded={};unknown=0
        for item in available:
            episodes=set(releases.normalized_episodes(item['release'],rule));reason=releases.admit(item,rule)
            if not episodes or any(e<=0 or e>9999 for e in episodes):reason=reason or '集号未确认';unknown+=not bool(episodes)
            if not reason and action!='upgrade' and episodes&(owned|pending):reason='已有剧集' if episodes&owned else '正在处理'
            if not reason and targets is not None and not episodes&targets:reason='无需处理'
            if not reason and action=='upgrade' and item['key'] in owned_hashes:reason='已有相同版本'
            if reason:
                excluded[reason]=excluded.get(reason,0)+1;continue
            item={**item,'episodes':episodes};eligible.append(item)
            release=item['release'];gid=release['group_id']
            group=groups.setdefault(gid,{'subgroupId':gid,'subgroupName':release['group'] or '未标注字幕组','resources':[]})
            group['resources'].append({**item['resource'],'release':release,'episodes':sorted(episodes)})
        for group in groups.values():group['episode_count']=len({e for r in group['resources'] for e in r['episodes']})
        considered=[item for item in eligible if selected is None or item['key'] in selected]
        chosen=[];uncovered=set().union(*(item['episodes'] for item in considered)) if considered else set()
        # Select one permitted release per episode; a batch may only cover unowned episodes.
        grouped=self.grouped(available)
        for item in sorted(considered,key=lambda it:releases.priority(it,rule)):
            if not item['episodes']&uncovered:continue
            chosen.append({**item,'sources':grouped[item['key']]['sources']});uncovered-=item['episodes']
        items=[{'release_id':item['key'],'title':item['title'],'episodes':sorted(item['episodes'])} for item in chosen]
        canonical=[user['id'],bid,action,destination,rule,items,snapshot['digest'],sorted(selected) if selected is not None else None]
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if 'digest' in body:
            if not snapshot['cloud_verified']:raise AppError('INVENTORY_UNAVAILABLE',snapshot['error'] or '库存未确认，请刷新',409)
            if digest!=body['digest']:raise AppError('LIBRARY_PLAN_CHANGED','库存或资源已变化，请重新确认',409)
            return self.queue(user,bid,chosen,rule,destination=destination,action=action,context=context,snapshot=snapshot)
        return {'items':items,'groups':list(groups.values()),'digest':digest,'destination':destination,'action':action,
                'scope_key':context['scope_key'],'owned_episodes':sorted(owned),'pending_episodes':sorted(pending),
                'missing_episodes':sorted(known-owned-pending),'available_episodes':sorted({e for item in eligible for e in item['episodes']}),
                'selected_episodes':sorted({e for item in chosen for e in item['episodes']}),
                'inventory_verified':snapshot['cloud_verified'],'inventory_error':snapshot['error'],'excluded':excluded,'unknown_resources':unknown}
