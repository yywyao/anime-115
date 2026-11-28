"""Shared, durable Mikan-page and RSS following pipeline."""
import hashlib,html,json,re,secrets,threading,time,urllib.parse
import xml.etree.ElementTree as ET
from .errors import AppError,ProviderError
from .torrent import torrent_magnet
from .workflow import normalize_magnet
from . import releases
from .feed_sources import FeedSources
from .feed_matching import bound_reason
from .inventory import work_scope

class RSS:
    def __init__(self,store,config,catalog,workflow):
        self.store,self.config,self.catalog,self.workflow=store,config,catalog,workflow
        self.lock=threading.RLock();self.stop_event=threading.Event();self.worker=None
        self.feeds=FeedSources(config,catalog);self.inventory=getattr(workflow,'inventory',None)

    def source(self,body):
        raw=str(body.get('rss_url') or '').strip()
        bid=str(body.get('bangumi_id') or '');sg=str(body.get('subgroup_id') or '0')
        if raw:source=self.feeds.normalize(raw)
        elif body.get('provider') and body.get('work_id'):
            source=self.feeds.search(self.catalog.work(body['work_id']),body['provider'],sg)
        else:
            if not bid.isdigit() or not sg.isdigit():raise AppError('INVALID_FEED','请选择作品或填写 RSS 链接')
            source=self.feeds.normalize(self.config.mikan_url+'/RSS/Bangumi?'+urllib.parse.urlencode({'bangumiId':bid,**({'subgroupid':sg} if sg!='0' else {})}))
        if source.mikan_id and bid and bid!=source.mikan_id:raise AppError('FEED_WORK_MISMATCH','作品与 RSS 不一致')
        path=source.url
        if source.provider=='mikan':
            parsed=urllib.parse.urlsplit(path);path=parsed.path+('?' + parsed.query if parsed.query else '')
        return source.mikan_id or source.key,source.group_id,source.kind if source.provider=='mikan' else source.provider,path

    def entries(self,bid,sg,path=None):
        if path:
            source=self.feeds.normalize(urllib.parse.urljoin(self.config.mikan_url,path))
            return self.feeds.entries(source)
        source=self.feeds.normalize(self.config.mikan_url+'/RSS/Bangumi?'+urllib.parse.urlencode({'bangumiId':bid,**({'subgroupid':sg} if str(sg)!='0' else {})}))
        return self.feeds.entries(source,self.catalog.rss_bytes(bid,sg))

    def cached_source(self,resource_hash,bid='',group_id=''):
        rows=self.store.all('SELECT * FROM release_sources WHERE resource_hash=?',(resource_hash,))
        rows=[r for r in rows if (not bid or r['bangumi_id']==bid) and (not group_id or r['group_id']==group_id)]
        return rows[0] if len(rows)==1 else None

    def identify(self,item):
        out={**item,'release':dict(item['release'])};r=out['release'];bid=out.get('bangumi_id','')
        source=self.cached_source(out.get('resource_hash',''),bid,r['group_id']) if bid or not out.get('episode_path') else None
        if source:
            if bid and bid!=source['bangumi_id']:raise ProviderError('RSS_WORK_MISMATCH','资源作品标识冲突')
            bid=out['bangumi_id']=source['bangumi_id'];r['group_id']=source['group_id']
        if bid and not r['group_id'] and r['group']:
            matches=self.store.all('SELECT group_id FROM work_groups WHERE bangumi_id=? AND name=?',(bid,r['group']))
            if len(matches)==1:r['group_id']=matches[0]['group_id']
        if out.get('episode_path') and (not bid or not r['group_id']):
            page=self.catalog.fetch(out['episode_path'],600)
            bids=set(re.findall(r'/Home/Bangumi/(\d+)',page));groups=set(re.findall(r'/Home/PublishGroup/(\d+)',page))
            if len(bids)==1:
                found=next(iter(bids))
                if bid and bid!=found:raise ProviderError('RSS_WORK_MISMATCH','RSS 作品标识冲突')
                bid=found;out['bangumi_id']=found
            if bid and not r['group_id']:
                # PublishGroup ids and RSS subgroup ids are distinct namespaces.
                self.catalog.mikan_detail(bid)
                source=self.cached_source(out.get('resource_hash',''),bid)
                if source and source['bangumi_id']==bid:r['group_id']=source['group_id']
                elif len(groups)==1:
                    mapped=self.store.all('SELECT group_id FROM group_publishers WHERE bangumi_id=? AND publisher_id=?',(bid,next(iter(groups))))
                    if len(mapped)==1:r['group_id']=mapped[0]['group_id']
        if bid and not r['group_id'] and r['group']:
            matches=self.store.all('SELECT group_id FROM work_groups WHERE bangumi_id=? AND name=?',(bid,r['group']))
            if len(matches)==1:r['group_id']=matches[0]['group_id']
        if bid and r['group_id']:
            row=self.store.one('SELECT name FROM work_groups WHERE bangumi_id=? AND group_id=?',(bid,r['group_id']))
            if row:r['group']=row['name']
        if bid:
            rows=self.catalog.seed_rows('WHERE mikan_id=?',(bid,),1000)
            work=self.catalog.mikan_work(rows)
            if work:
                mapping=work.get('tmdb_mapping') or {};status=mapping.get('status')
                # RSS resources inherit only an exact, already-confirmed
                # catalog crosswalk. Parsed titles and fuzzy similarity never
                # establish TMDB/TVDB identity.
                if status in {'linked','episode_scope'} and str(mapping.get('id') or '').isdigit():
                    providers=work.get('provider_ids') or {}
                    crosswalk={'bangumi':bid,'tmdb':{'type':mapping.get('type') or 'tv','id':str(mapping['id'])}}
                    if work.get('tmdb_season') is not None:crosswalk['tmdb']['season']=work['tmdb_season']
                    tvdb=str(providers.get('tvdb') or '')
                    if tvdb.isdigit():
                        crosswalk['tvdb']={'id':tvdb}
                        if work.get('tvdb_season') is not None:crosswalk['tvdb']['season']=work['tvdb_season']
                    out['provider_crosswalk']=crosswalk
                    if work.get('tmdb_season') is not None:out['identity_season']=work['tmdb_season']
                    out['scope_key']=work_scope(self.catalog,work);out['work_id']=str(work.get('canonical_id') or work.get('id') or bid)
        return out

    def sources(self,sub):
        values=[]
        if sub.get('source_encrypted'):values.append(self.store.decrypt(sub['source_encrypted']))
        if sub.get('id'):
            values.extend(self.store.decrypt(row['source_encrypted']) for row in self.store.all('SELECT source_encrypted FROM subscription_sources WHERE subscription_id=?',(sub['id'],)))
        if not values and str(sub.get('bangumi_id','')).isdigit():values.append('/RSS/Bangumi?'+urllib.parse.urlencode({'bangumiId':sub['bangumi_id'],**({'subgroupid':sub['subgroup_id']} if sub['subgroup_id']!='0' else {})}))
        result={}
        for value in values:
            source=self.feeds.normalize(urllib.parse.urljoin(self.config.mikan_url,value));result[source.key]=source
        return list(result.values())

    def read(self,sub):
        rule=releases.rules(json.loads(sub['rules_json']));grouped={};success=0;failure=None
        for source in self.sources(sub):
            try:items=self.feeds.entries(source);success+=1
            except AppError as error:failure=error;continue
            for item in items:grouped.setdefault(item['key'],[]).append(item)
        if failure and not success:raise failure
        result=[]
        for sources in grouped.values():
            admitted=[item for item in sources if not releases.admit(item,rule)]
            primary=min(admitted or sources,key=lambda item:releases.priority(item,rule))
            result.append({**primary,'sources':sources})
        return result

    def identify_bound(self,item,sub):
        item=self.identify(item) if item.get('provider','mikan')=='mikan' else {**item,'release':dict(item['release'])}
        ident=sub.get('work_id')
        if not ident:return item
        context=sub.get('_work_context')
        if context is None:context=self.inventory.context(ident);sub['_work_context']=context
        expected=context['scope_key'];actual=item.get('scope_key')
        if actual and actual!=expected:item['binding_error']='作品或季度不匹配';return item
        if item.get('provider','mikan')=='mikan' and item.get('bangumi_id'):
            if context['mikan_id'] and str(item['bangumi_id'])!=context['mikan_id']:
                item['binding_error']='作品不匹配';return item
        elif not actual:
            reason=bound_reason(item,context,self.catalog)
            if reason:item['binding_error']=reason;return item
        item['scope_key']=expected;item['work_id']=context['work_id'];item['identity_season']=context['season']
        item['bangumi_id']=context['mikan_id']
        scope=self.catalog.tmdb_scope(context['work'])
        if scope:item['provider_crosswalk']={**item.get('provider_crosswalk',{}),'tmdb':{'type':scope[0],'id':str(scope[1]),'season':scope[2]}}
        return item

    def inventory_reason(self,user,item,sub,rule,memo=None):
        destination='vps' if rule['download_to_vps'] else 'cloud'
        ident=item.get('work_id') or sub.get('work_id') or item.get('bangumi_id')
        if not ident or not self.inventory:return '',None,None
        key=(user['id'],ident)
        if memo is not None and key in memo:context,snapshot=memo[key]
        else:
            context,snapshot=self.inventory.snapshot(user,ident)
            if memo is not None:memo[key]=(context,snapshot)
        episodes=set(releases.normalized_episodes(item['release'],rule))
        if not episodes:return '集号未确认',context,snapshot
        owned=self.inventory.owned(snapshot,destination)
        if rule['dedupe']!='episode':
            owned={e for file in snapshot[destination] for e in file['episodes'] if
                (rule['dedupe']=='hash' and item.get('infohash')==file['infohash']) or
                (rule['dedupe']=='episode_version' and file['release']['version']==item['release']['version'])}
        if episodes&owned:return '已有剧集',context,snapshot
        # Persisted claims in Workflow make the final atomic decision. Pending
        # cloud work can be reused for a separate VPS request.
        if rule['dedupe']=='episode' and episodes&set(snapshot['pending'][destination]):return '正在处理',context,snapshot
        return '',context,snapshot

    def options(self,ident,bid=None):
        work=self.catalog.work(ident) if ident else {};bid=str(bid or work.get('mikan_id') or work.get('bangumiId') or '')
        if not bid.isdigit():return {'source_required':True,'work':work,'groups':[],'versions':releases.VERSIONS}
        detail=self.catalog.mikan_detail(bid);groups=[];versions=set()
        for g in detail['subgroups']:
            labels=sorted({releases.parse(r['title'],g['subgroupId'],g['subgroupName'])['version'] for r in g['resources']})
            groups.append({'id':g['subgroupId'],'name':g['subgroupName'],'versions':labels,'resources':g['resourceCount']});versions.update(labels)
        return {'source_required':False,'bangumi_id':bid,'work':{**work,'title':work.get('title') or detail['title']},'groups':groups,'versions':releases.VERSIONS,'observed_versions':sorted(versions)}

    def prepare(self,body):
        if set(body)-{'rss_url','bangumi_id','subgroup_id','work_id','title','rules','include_existing','provider'}:raise AppError('INVALID_FEED','追新包含不支持的字段')
        rule=releases.rules(body.get('rules'));source_body=dict(body)
        if not body.get('rss_url') and body.get('provider','mikan')=='mikan' and str(body.get('subgroup_id') or '0')=='0' and len(rule['group_ids'])==1 and rule['group_ids'][0].isdigit():source_body['subgroup_id']=rule['group_ids'][0]
        bid,sg,kind,path=self.source(source_body)
        source=self.feeds.normalize(urllib.parse.urljoin(self.config.mikan_url,path))
        if source.provider=='mikan' and source.group_id!='0':
            detail=self.catalog.mikan_detail(source.mikan_id)
            if source.group_id not in {str(g['subgroupId']) for g in detail['subgroups']}:
                raise AppError('FEED_GROUP_MISMATCH','作品中未找到该字幕组')
        work_id=str(body.get('work_id') or (bid if bid.isdigit() else ''))[:150]
        work=self.catalog.work(work_id) if work_id else {}
        if kind not in {'bangumi','personal'} and not work_id:raise AppError('TMDB_WORK_REQUIRED','请先选择动漫',409)
        if work_id:
            if source.mikan_id and work.get('mikan_id') and str(work['mikan_id'])!=source.mikan_id:raise AppError('FEED_WORK_MISMATCH','作品与 RSS 不一致')
            scope=self.catalog.tmdb_scope(work)
            if kind not in {'bangumi','personal'} and not scope:raise AppError('TMDB_MATCH_REQUIRED','请先关联 TMDB 作品',409)
            if scope and scope[0]=='tv':
                if rule['season'] not in (None,scope[2]):raise AppError('FEED_SEASON_MISMATCH','季号与作品不一致')
                rule['season']=scope[2]
            key=work_scope(self.catalog,work)
            bid=str(work.get('mikan_id') or work.get('bangumiId') or ('work:'+hashlib.sha256(key.encode()).hexdigest()[:24]))
        else:key=''
        if sg!='0':
            if rule['group_ids'] and rule['group_ids']!=[sg]:raise AppError('FEED_GROUP_MISMATCH','RSS 与所选字幕组不一致')
            rule['group_ids']=[sg]
        title=work.get('series_title') or work.get('homeTitle') or work.get('title') or ('个人 RSS' if kind=='personal' else self.feeds.names.get(source.provider,'RSS'))
        return {'bangumi_id':bid,'subgroup_id':sg,'source_kind':kind,'source_encrypted':self.store.encrypt(path),'rules_json':json.dumps(rule,ensure_ascii=False),
                'work_id':work_id,'scope_key':key,'title':title[:300]}

    def configuration(self,user,body):
        try:sub=self.prepare(body)
        except AppError as error:
            if error.code=='TMDB_WORK_REQUIRED':return {'source_required':True,'work_required':True,'groups':[],'versions':releases.VERSIONS}
            raise
        return self.configuration_for(sub)

    def configuration_for(self,sub):
        work=self.catalog.work(sub['work_id']) if sub['work_id'] else {}
        if sub['source_kind']=='bangumi':result=self.options(sub['work_id'],sub['bangumi_id'])
        else:result={'source_required':False,'groups':[],'versions':releases.VERSIONS}
        if sub['source_kind']!='bangumi' or len(self.sources(sub))>1:
            groups={str(g['id']):g for g in result['groups']}
            for item in self.read(sub):
                release=item['release'];ident=release['group_id']
                if not ident:continue
                entry=groups.setdefault(ident,{'id':ident,'name':release['group'] or '未标注字幕组','versions':[],'resources':0})
                entry['resources']+=1
                if release['version'] not in entry['versions']:entry['versions'].append(release['version'])
            result={**result,'groups':list(groups.values())}
        return {**result,'work':work,'bangumi_id':sub['bangumi_id'],'work_id':sub['work_id'],'scope_key':sub['scope_key'],
                'source_kind':sub['source_kind'],'source_subgroup_id':sub['subgroup_id'],'title':sub['title']}

    def create(self,user,body):
        prepared=self.prepare(body);items=self.read(prepared);sid=secrets.token_hex(16);now=time.time()
        source=self.sources(prepared)[0]
        with self.store.transaction() as c:
            old=c.execute('SELECT * FROM subscriptions WHERE user_id=? AND (bangumi_id=? OR (scope_key<>\'\' AND scope_key=?)) AND subgroup_id=? AND removed=0',
                          (user['id'],prepared['bangumi_id'],prepared['scope_key'],prepared['subgroup_id'])).fetchone()
            if old:
                sid=old['id']
                known=c.execute('SELECT 1 FROM subscription_sources WHERE subscription_id=? AND source_key=?',(sid,source.key)).fetchone()
                if known or old['source_encrypted'] and self.store.decrypt(old['source_encrypted'])==self.store.decrypt(prepared['source_encrypted']):
                    return {'id':sid,'existing':True,'source_added':False}
            archived=c.execute('SELECT id FROM subscriptions WHERE user_id=? AND bangumi_id=? AND subgroup_id=?',(user['id'],prepared['bangumi_id'],prepared['subgroup_id'])).fetchone()
            if old:pass
            elif archived:
                sid=archived['id'];c.execute('DELETE FROM subscription_sources WHERE subscription_id=?',(sid,))
                if body.get('include_existing') is True:
                    c.execute("DELETE FROM subscription_releases WHERE subscription_id=? AND status='BASELINE'",(sid,))
                    c.execute("DELETE FROM subscription_items WHERE subscription_id=? AND transfer_id='IGNORED_BASELINE'",(sid,))
                c.execute('UPDATE subscriptions SET enabled=1,removed=0,revision=revision+1,work_id=?,title=?,rules_json=?,source_encrypted=?,source_kind=?,scope_key=?,accept_after=? WHERE id=?',
                    (prepared['work_id'],prepared['title'],prepared['rules_json'],prepared['source_encrypted'],prepared['source_kind'],prepared['scope_key'],0 if body.get('include_existing') is True else now,sid))
            else:c.execute('INSERT INTO subscriptions(id,user_id,work_id,bangumi_id,subgroup_id,title,initialized,last_polled,rules_json,source_encrypted,source_kind,scope_key,accept_after) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?)',
                (sid,user['id'],prepared['work_id'],prepared['bangumi_id'],prepared['subgroup_id'],prepared['title'],now,prepared['rules_json'],prepared['source_encrypted'],prepared['source_kind'],prepared['scope_key'],0 if body.get('include_existing') is True else now))
            c.execute('INSERT OR IGNORE INTO subscription_sources VALUES(?,?,?,?,?)',(sid,source.key,source.provider,prepared['source_encrypted'],now))
            if body.get('include_existing') is not True:
                for entry in items:
                    c.execute('INSERT OR IGNORE INTO subscription_items VALUES(?,?,?)',(sid,entry['key'],'IGNORED_BASELINE'))
                    c.execute('INSERT OR IGNORE INTO subscription_releases VALUES(?,?,?,?,?,?,?,?)',(sid,entry['key'],json.dumps(entry,ensure_ascii=False),'BASELINE','创建前资源',None,now,now))
        return {'id':sid,'existing':bool(old),'source_added':bool(old),'baseline_items':0 if body.get('include_existing') is True else len(items),'downloads_history':body.get('include_existing') is True}

    def authorize(self,user,sid):
        row=self.store.one('SELECT * FROM subscriptions WHERE id=? AND removed=0',(sid,))
        if not row or (row['user_id']!=user['id'] and not user['is_admin']):raise AppError('SUBSCRIPTION_NOT_FOUND','追新不存在',404)
        return row

    def public(self,row):
        out={k:row[k] for k in ['id','work_id','bangumi_id','subgroup_id','title','enabled','last_polled','last_error','source_kind','revision','user_id']}
        out['rules']=releases.rules(json.loads(row['rules_json']));out['username']=row.get('username','')
        out['scope_key']=row.get('scope_key','')
        out['providers']=sorted({source.provider for source in self.sources(row)})
        if row['work_id']:
            try:
                work=self.catalog.work(row['work_id']);out['work']={key:work.get(key) for key in ('id','canonical_id','title','homeTitle','series_title','mikan_id','tmdb_id','tmdb_season','tmdb_mapping')}
                out['scope_key']=work_scope(self.catalog,work);out['title']=work.get('homeTitle') or work.get('title') or row['title']
            except AppError:pass
        names={group['group_id']:group['name'] for group in self.store.all('SELECT group_id,name FROM work_groups WHERE bangumi_id=?',(row['bangumi_id'],))}
        for group in self.store.all("SELECT DISTINCT json_extract(payload,'$.release.group_id') id,json_extract(payload,'$.release.group') name FROM subscription_releases WHERE subscription_id=? LIMIT 1000",(row['id'],)):
            if group['id'] and group['name']:names.setdefault(group['id'],group['name'])
        out['groups']=[{'id':ident,'name':names.get(ident,'未标注字幕组')} for ident in out['rules']['group_ids']]
        out['counts']={r['status']:r['n'] for r in self.store.all('SELECT status,count(*) n FROM subscription_releases WHERE subscription_id=? GROUP BY status',(row['id'],))}
        return out

    def list(self,user,all_users=False):
        where='' if all_users and user['is_admin'] else ' AND s.user_id=?';args=() if not where else (user['id'],)
        rows=self.store.all('SELECT s.*,u.username FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.removed=0'+where+' ORDER BY s.rowid DESC',args)
        return [self.public(row) for row in rows]

    def settings(self):
        binding=self.store.binding(False)
        return {'paused':self.store.setting('rss_paused','0')=='1','interval_seconds':int(self.store.setting('rss_interval',self.config.rss_interval)),'connection_ready':bool(binding and binding['ready'])}

    def configure(self,body):
        if set(body)-{'paused','interval_seconds'}:raise AppError('INVALID_SETTINGS','设置无效')
        if 'interval_seconds' in body:
            n=body['interval_seconds']
            if isinstance(n,bool) or not isinstance(n,int) or not 60<=n<=86400:raise AppError('INVALID_INTERVAL','检查间隔应为 60–86400 秒')
            self.store.set_setting('rss_interval',n)
        if 'paused' in body:
            if not isinstance(body['paused'],bool):raise AppError('INVALID_SETTINGS','暂停设置无效')
            self.store.set_setting('rss_paused','1' if body['paused'] else '0')
        return self.settings()

    def update(self,user,sid,body):
        current=self.authorize(user,sid)
        if set(body)-{'enabled','rules','title','remove'}:raise AppError('INVALID_RULES','修改包含不支持的字段')
        values={}
        if 'rules' in body:
            rule=self.bound_rules(current,body['rules'])
            if current['subgroup_id']!='0':
                if rule['group_ids'] and rule['group_ids']!=[current['subgroup_id']]:
                    raise AppError('FEED_GROUP_MISMATCH','字幕组 RSS 与追新字幕组不一致')
                rule['group_ids']=[current['subgroup_id']]
            values['rules_json']=json.dumps(rule,ensure_ascii=False)
        if 'title' in body and not current['work_id']:values['title']=str(body['title'])[:300]
        if 'enabled' in body:
            if not isinstance(body['enabled'],bool):raise AppError('INVALID_RULES','开关无效')
            values['enabled']=int(body['enabled'])
        if body.get('remove') is True:values.update(enabled=0,removed=1)
        if values:
            with self.store.transaction() as c:
                c.execute('UPDATE subscriptions SET '+','.join(k+'=?' for k in values)+',revision=revision+1 WHERE id=?',(*values.values(),sid))
                if 'rules' in body:c.execute("UPDATE subscription_releases SET status='WAITING',reason='' WHERE subscription_id=? AND status IN ('FILTERED','DUPLICATE')",(sid,))
        return {'id':sid,'enabled':bool(values.get('enabled',self.store.one('SELECT enabled FROM subscriptions WHERE id=?',(sid,))['enabled']))}

    def toggle(self,user,sid,enabled):return self.update(user,sid,{'enabled':enabled})

    def bound_rules(self,sub,value):
        rule=releases.rules(value)
        if sub['work_id']:
            scope=self.catalog.tmdb_scope(self.catalog.work(sub['work_id']))
            if scope and scope[0]=='tv':
                if rule['season'] not in (None,scope[2]):raise AppError('FEED_SEASON_MISMATCH','季号与作品不一致')
                rule['season']=scope[2]
        return rule

    def preview(self,user,body,sid=None):
        sub=self.authorize(user,sid) if sid else self.prepare(body);rule=self.bound_rules(sub,body.get('rules',json.loads(sub['rules_json'])));rows=[];memo={}
        for item in self.read(sub)[:100]:
            item=self.identify_bound(item,sub);reason=item.get('binding_error') or releases.admit(item,rule)
            if any(not 0<=n<=9999 for n in releases.normalized_episodes(item['release'],rule)):reason='集号偏移无效'
            if sub['source_kind']=='personal' and not item.get('bangumi_id'):reason='作品未确认'
            prior=self.store.one('SELECT status FROM subscription_releases WHERE subscription_id=? AND item_key=?',(sid,item['key'])) if sid else None
            if not reason:reason,_,_=self.inventory_reason(user,item,sub,rule,memo)
            rows.append({**item,'admitted':not reason,'reason':reason,'previous':prior['status'] if prior else ''})
        rows.sort(key=lambda item:releases.priority(item,rule));seen=set()
        for item in rows:
            keys=self.claim_keys(item,rule)
            duplicate=any(k in seen or self.store.one('SELECT 1 n FROM subscription_claims WHERE user_id=? AND episode_key=?',(sub.get('user_id',user['id']),k)) for k in keys)
            if item['admitted'] and duplicate:item.update(admitted=False,reason='重复剧集')
            if item['admitted']:seen.update(keys)
        return {'items':[{k:v for k,v in i.items() if k in {'key','title','release','admitted','reason','previous','bangumi_id','provider_crosswalk'}} for i in rows],'dry_run':True}

    @staticmethod
    def claim_keys(item,rule):
        return [key+(':vps' if rule['download_to_vps'] else '') for key in releases.episode_keys(item,rule)]

    def history(self,user,sid):
        self.authorize(user,sid)
        rows=self.store.all('SELECT payload,status,reason,transfer_id,updated_at FROM subscription_releases WHERE subscription_id=? ORDER BY updated_at DESC LIMIT 100',(sid,))
        return {'items':[{'title':json.loads(r['payload'])['title'],'release':json.loads(r['payload'])['release'],**{k:r[k] for k in ['status','reason','transfer_id','updated_at']}} for r in rows]}

    def decision(self,sid,key,status,reason='',transfer_id=None):
        with self.store.transaction() as c:c.execute('UPDATE subscription_releases SET status=?,reason=?,transfer_id=?,updated_at=? WHERE subscription_id=? AND item_key=?',(status,reason,transfer_id,time.time(),sid,key))

    def poll(self,sub):
        with self.lock:
            sub=self.store.one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
            if not sub or not sub['enabled'] or sub['removed'] or self.settings()['paused'] or self.store.setting('update_maintenance')=='1':return
            try:
                items=self.read(sub);rule=releases.rules(json.loads(sub['rules_json']));now=time.time()
                user=self.store.one('SELECT * FROM users WHERE id=? AND disabled=0',(sub['user_id'],))
                if not user:return
                with self.store.transaction() as c:
                    for item in items:
                        baseline=c.execute('SELECT transfer_id FROM subscription_items WHERE subscription_id=? AND item_key IN (?,?)',(sub['id'],item['key'],item['legacy_key'])).fetchone()
                        if baseline and baseline['transfer_id'] not in ('PENDING',None):continue
                        before=item.get('published_at') and item['published_at']<sub.get('accept_after',0)
                        c.execute('INSERT OR IGNORE INTO subscription_releases VALUES(?,?,?,?,?,?,?,?)',(sub['id'],item['key'],json.dumps(item,ensure_ascii=False),'BASELINE' if before else 'WAITING','创建前资源' if before else '',None,now,now))
                pending=self.store.all("SELECT * FROM subscription_releases WHERE subscription_id=? AND status IN ('WAITING','ERROR','PENDING') ORDER BY first_seen LIMIT 500",(sub['id'],));candidates=[];first_seen={}
                for row in pending:
                    item=self.identify_bound(json.loads(row['payload']),sub);reason=item.get('binding_error') or releases.admit(item,rule)
                    if any(not 0<=n<=9999 for n in releases.normalized_episodes(item['release'],rule)):reason='集号偏移无效'
                    if reason:self.decision(sub['id'],item['key'],'FILTERED',reason);continue
                    if not item.get('bangumi_id') and sub['source_kind']=='personal':self.decision(sub['id'],item['key'],'ERROR','作品未确认');continue
                    candidates.append(item)
                    for key in self.claim_keys(item,rule):first_seen[key]=min(first_seen.get(key,now),row['first_seen'])
                candidates=[item for item in candidates if all(now-first_seen[key]>=rule['wait_minutes']*60 for key in self.claim_keys(item,rule))]
                candidates.sort(key=lambda item:releases.priority(item,rule))
                memo={}
                for item in candidates:
                    if self.stop_event.is_set() or self.settings()['paused']:return
                    keys=self.claim_keys(item,rule)
                    reason,context,snapshot=self.inventory_reason(user,item,sub,rule,memo)
                    if reason:
                        self.decision(sub['id'],item['key'],'DUPLICATE' if reason in {'已有剧集','正在处理'} else 'ERROR',reason);continue
                    with self.store.transaction() as c:
                        current=c.execute('SELECT enabled,removed,revision FROM subscriptions WHERE id=?',(sub['id'],)).fetchone()
                        if not current or not current['enabled'] or current['removed'] or current['revision']!=sub['revision']:return
                        claims=[c.execute('SELECT * FROM subscription_claims WHERE user_id=? AND episode_key=?',(user['id'],key)).fetchone() for key in keys]
                        if any(r and (r['subscription_id']!=sub['id'] or r['item_key']!=item['key']) for r in claims):duplicate=True
                        else:
                            duplicate=False
                            for key in keys:c.execute('INSERT OR IGNORE INTO subscription_claims VALUES(?,?,?,?,NULL)',(user['id'],key,sub['id'],item['key']))
                    if duplicate:self.decision(sub['id'],item['key'],'DUPLICATE','重复剧集');continue
                    self.decision(sub['id'],item['key'],'PENDING')
                    try:
                        episodes=releases.normalized_episodes(item['release'],rule)
                        reuse=self.inventory.reusable(snapshot,episodes,rule) if snapshot and rule['download_to_vps'] else None
                        if snapshot and not snapshot['cloud_verified']:raise AppError('INVENTORY_UNAVAILABLE',snapshot['error'] or '库存未确认',409)
                        source=next((s for s in self.sources(sub) if s.key==item.get('source_key')),None)
                        magnet=self.workflow.authorize(user,reuse['transfer_id'])['magnet'] if reuse else item['magnet'] or torrent_magnet(self.feeds.torrent(source,item['torrent']),item['title'])
                        work_id=item.get('work_id') or sub['work_id'] or item['bangumi_id']
                        metadata={'release':item['release'],'rules':rule,'work_id':work_id,'bangumi_id':item['bangumi_id'],'resource_hash':item.get('resource_hash','')}
                        if context:
                            metadata['scope_key']=context['scope_key']
                            metadata['acquisition']={'scope_key':context['scope_key'],'episodes':episodes,'destination':'vps' if rule['download_to_vps'] else 'cloud',
                                'dedupe':rule['dedupe'],'version':item['release']['version'],'verified_tasks':list({f['transfer_id'] for f in snapshot['cloud']}|set(snapshot.get('pending_tasks',[])))}
                        job=self.workflow.create(user,magnet,item['title'],work_id,subscription_id=sub['id'],subscription_revision=sub['revision'],naming=metadata)
                        if job is None:return
                        with self.store.transaction() as c:
                            c.execute('INSERT OR REPLACE INTO subscription_items VALUES(?,?,?)',(sub['id'],item['key'],job['id']))
                            c.execute('UPDATE subscription_claims SET transfer_id=? WHERE user_id=? AND subscription_id=? AND item_key=?',(job['id'],user['id'],sub['id'],item['key']))
                            if rule['download_to_vps']:
                                old=c.execute('SELECT episodes_json FROM following_downloads WHERE subscription_id=? AND transfer_id=?',(sub['id'],job['id'])).fetchone()
                                combined=sorted(set(episodes)|set(json.loads(old['episodes_json']) if old else []))
                                c.execute('INSERT INTO following_downloads(subscription_id,transfer_id,revision,episodes_json) VALUES(?,?,?,?) ON CONFLICT(subscription_id,transfer_id) DO UPDATE SET episodes_json=excluded.episodes_json,submitted=0,revision=excluded.revision',
                                          (sub['id'],job['id'],sub['revision'],json.dumps(combined)))
                        self.decision(sub['id'],item['key'],'TRANSFERRED','已创建转存',job['id'])
                    except AppError as e:self.decision(sub['id'],item['key'],'ERROR',e.message)
                with self.store.transaction() as c:c.execute('UPDATE subscriptions SET last_polled=?,last_error=NULL WHERE id=?',(time.time(),sub['id']))
            except AppError as e:
                with self.store.transaction() as c:c.execute('UPDATE subscriptions SET last_polled=?,last_error=? WHERE id=?',(time.time(),e.message,sub['id']))

    def enqueue_downloads(self,library):
        if self.settings()['paused'] or self.store.setting('update_maintenance')=='1':return
        with self.lock:
            pending=self.store.all("""SELECT d.subscription_id,d.transfer_id,d.revision,d.episodes_json,u.id,u.is_admin,s.work_id
                FROM following_downloads d JOIN subscriptions s ON s.id=d.subscription_id
                JOIN transfers t ON t.id=d.transfer_id JOIN users u ON u.id=s.user_id
                LEFT JOIN transfer_naming n ON n.transfer_id=t.id
                WHERE d.submitted=0 AND d.next_retry<=? AND s.enabled=1 AND s.removed=0 AND s.revision=d.revision
                AND t.state='FILES_READY' AND u.disabled=0 AND (n.auto_rename IS NOT 1 OR n.status!='PENDING')
                ORDER BY d.rowid LIMIT 10""",(time.time(),))
            for row in pending:
                if self.settings()['paused']:return
                layout=getattr(self.workflow,'layout',None)
                if layout and not layout.ready(row['transfer_id']):continue
                sub=self.store.one('SELECT enabled,removed,revision FROM subscriptions WHERE id=?',(row['subscription_id'],))
                if not sub or not sub['enabled'] or sub['removed'] or sub['revision']!=row['revision']:continue
                try:
                    selection={};episodes=set(json.loads(row['episodes_json']))
                    if episodes and self.inventory:
                        ident=row['work_id'] or self.workflow.authorize({'id':row['id'],'is_admin':row['is_admin']},row['transfer_id'])['work_id']
                        _,snapshot=self.inventory.snapshot({'id':row['id'],'is_admin':row['is_admin']},ident)
                        selection={'file_ids':[file['file_id'] for file in snapshot['cloud'] if file['transfer_id']==row['transfer_id'] and episodes.intersection(file['episodes'])]}
                        if not selection['file_ids']:raise AppError('FILES_NOT_READY','等待文件核对',409)
                    queued=library.enqueue({'id':row['id'],'is_admin':row['is_admin']},row['transfer_id'],selection,
                                           following=(row['subscription_id'],row['revision']))
                except AppError:
                    with self.store.transaction() as c:c.execute('UPDATE following_downloads SET next_retry=? WHERE subscription_id=? AND transfer_id=?',
                              (time.time()+60,row['subscription_id'],row['transfer_id']))
                else:
                    if queued is None:continue
                    with self.store.transaction() as c:c.execute('UPDATE following_downloads SET submitted=1 WHERE subscription_id=? AND transfer_id=?',
                              (row['subscription_id'],row['transfer_id']))

    def tick(self):
        settings=self.settings()
        if settings['paused'] or not settings['connection_ready']:return
        for sub in self.store.all('SELECT * FROM subscriptions WHERE enabled=1 AND removed=0 AND last_polled<? ORDER BY last_polled LIMIT 5',(time.time()-settings['interval_seconds'],)):self.poll(sub)

    def discover_groups(self):
        if not self.config.catalog_auto_update:return
        prior=self.store.one("SELECT * FROM catalog_sync WHERE key='group-discovery'")
        if prior and prior['next_due']>time.time():return
        works=sorted(self.catalog.all_works(),key=lambda w:int(w.get('year') or 0),reverse=True);done=set()
        for work in works:
            if self.stop_event.is_set() or len(done)>=3:break
            bid=str(work.get('mikan_id') or '')
            if not bid.isdigit() or bid in done:continue
            checked=self.store.one('SELECT next_due FROM catalog_sync WHERE key=?',('group:'+bid,))
            if checked and checked['next_due']>time.time():continue
            done.add(bid)
            try:
                detail=self.catalog.mikan_detail(bid)
                self.catalog.sync_result('group:'+bid,{'groups':detail['subgroupsCount']},3600 if int(work.get('year') or 0)>=self.catalog.current()['year']-1 else 7*86400)
            except AppError:self.catalog.sync_result('group:'+bid,{'status':'retry'},86400,True)
        self.catalog.sync_result('group-discovery',{'checked':len(done)},900)

    def start(self):
        if self.worker:return
        def run():
            while not self.stop_event.is_set():
                try:self.tick();self.discover_groups()
                except Exception:pass
                self.stop_event.wait(5)
        self.worker=threading.Thread(target=run,name='rss-following',daemon=True);self.worker.start()

    def close(self):
        self.stop_event.set()
        if self.worker:self.worker.join(timeout=25)
