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
    def __init__(self, catalog, workflow, library):
        self.catalog = catalog
        self.workflow = workflow
        self.library = library

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

    def queue(self, user, bid, items, rule):
        work=self.catalog.work(bid)
        scope=self.catalog.tmdb_scope(work)
        if scope and scope[0]=='tv' and scope[2] is not None:
            if rule['season'] not in (None,scope[2]):raise AppError('LIBRARY_SEASON_MISMATCH','季号与当前作品不一致',409)
            rule={**rule,'season':scope[2]}
        prepared = []
        for item in items:
            resolved = dict(self.magnet(source) for source in item['sources'])
            if len(resolved) != 1:
                raise AppError('RESOURCE_CONFLICT', '资源来源不一致，请重新选择', 409)
            prepared.append((next(iter(resolved.values())), item))
        tasks = {}
        with self.workflow.connection_lock:
            for magnet, item in prepared:
                release=dict(item['release'])
                sources=[{'group_id':source['release']['group_id'],'group':source['release']['group']}
                         for source in item['sources']]
                if len({source['group_id'] for source in sources})>1 and not rule['group_ids']:
                    release.update(group_id='',group='')
                metadata = {'release':release,'sources':sources,'rules':rule,'work_id':bid,
                            'bangumi_id':bid,'resource_hash':item['key']}
                task = self.workflow.create(user, magnet, item['title'], bid, enqueue_only=True, naming=metadata)
                tasks[task['id']] = task
        return {'success': True, 'tasks': list(tasks.values())}

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

    def selected(self, user, bid, keys, rules=None):
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
        return self.queue(user, bid, chosen, rule)

    def action(self, user, body):
        if set(body) - {'mikan_id', 'action', 'rules', 'digest', 'release_ids'}:
            raise AppError('INVALID_LIBRARY_ACTION', '操作参数无效')
        bid = self.source_id(body.get('mikan_id'))
        action = body.get('action')
        if action not in ('missing', 'complete', 'upgrade'):
            raise AppError('INVALID_LIBRARY_ACTION', '请选择入库操作')
        rule = releases.rules(body.get('rules'))
        rule['allow_batches'] = True
        selected=set(self.release_ids(body['release_ids'])) if 'release_ids' in body else None
        if action == 'upgrade' and not any(rule[k] for k in ('group_ids', 'versions', 'resolutions')):
            raise AppError('UPGRADE_PREFERENCE_REQUIRED', '请选择优先使用的字幕组、字幕或分辨率')
        result = self.library.summary(user, [bid])
        entries = result.get('items', [])
        status = entries.get(bid, {}) if isinstance(entries, dict) else next(
            (item for item in entries if str(item.get('mikan_id')) == bid), {})
        work=self.catalog.work(bid)
        if str(work.get('mikan_id') or work.get('bangumiId') or '') == bid:
            scope=self.catalog.tmdb_scope(work)
            if scope and scope[0]=='tv' and scope[3] is None:
                if rule['season'] not in (None,scope[2]):
                    raise AppError('LIBRARY_SEASON_MISMATCH','筛选季号与当前作品不一致',409)
                rule['season']=scope[2]
        installed = set(status.get('episodes', []))
        target = set(status.get('missing_episodes', [])) if action == 'missing' else installed.copy()

        candidates = []
        expected=status.get('expected_episodes')
        expected=set(expected) if isinstance(expected,list) else set()
        available=list(self.candidates(bid))
        if selected and not selected<={item['key'] for item in available}:
            raise AppError('RESOURCE_NOT_FOUND','所选资源已变化，请重新选择',409)
        for item in available:
            if selected is not None and item['key'] not in selected:continue
            eps = set(releases.normalized_episodes(item['release'], rule))
            if expected:eps &= expected
            if eps and all(0 < n <= 9999 for n in eps) and not releases.admit(item, rule):
                candidates.append({**item, 'episodes': eps})
        if action == 'complete':
            # Collect all known episodes that have a release, including a known full-season batch.
            target = set().union(*(item['episodes'] for item in candidates)) - installed
        elif action == 'upgrade':
            target = installed

        ordered = sorted(candidates, key=lambda item: releases.priority(item, rule))
        grouped = self.grouped(available)
        chosen = []
        outstanding = target.copy()
        owned = set(status.get('release_hashes', []))
        while outstanding:
            eligible = [item for item in ordered if item['episodes'] & outstanding and item['key'] not in owned]
            if not eligible:
                break
            # Prefer a confirmed batch covering more targets when collecting or filling gaps.
            item = eligible[0] if action == 'upgrade' else max(eligible, key=lambda x: len(x['episodes'] & outstanding))
            chosen.append({**item, 'sources': grouped[item['key']]['sources']})
            outstanding -= item['episodes']
            owned.add(item['key'])

        public = [{'release_id': item['key'], 'title': item['title'], 'episodes': sorted(item['episodes'])}
                  for item in chosen]
        canonical = [user['id'], bid, action, rule, public, sorted(outstanding),sorted(selected) if selected is not None else None]
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if 'digest' in body:
            if digest != body['digest']:
                raise AppError('LIBRARY_PLAN_CHANGED', '资源或入库状态已变化，请重新预览', 409)
            return self.queue(user, bid, chosen, rule)
        return {'items': public, 'missing_episodes': sorted(outstanding), 'digest': digest}
