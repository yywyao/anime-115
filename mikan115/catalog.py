import datetime
import contextlib
import html
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.parse
from zoneinfo import ZoneInfo

from .errors import AppError,ProviderError
from .http_client import HTTP
from . import mikan_parser as parser
from .torrent import torrent_magnet
from .metadata import Metadata
from . import search as fuzzy
from .releases import parse as parse_release


class Catalog:
    def __init__(self,store,config,http=None,metadata=None):
        self.store,self.config,self.http=store,config,http or HTTP(timeout=4)
        self.seed=config.root/'seed'/'catalog.sqlite'
        self.metadata=metadata or Metadata(store)
        self._lock=threading.RLock();self._inflight={};self._fail_until={};self._closed=False
        self._search_index=None;self._updater=None;self._update_stop=threading.Event();self._references=None
        self.reconcile_public_seed()

    def reconcile_public_seed(self):
        if not self.seed.is_file():return
        with self.seed.open('rb') as source:version=hashlib.file_digest(source,'sha256').hexdigest()
        if self.store.setting('public_seed_version')==version:return
        ids=[r['id'] for r in self.seed_query('SELECT id FROM works')]
        with self.store.transaction() as db:
            # Only replace derived public rows now supplied by the newer seed.
            # Private accounts/tasks/favorites and runtime-only works are untouched.
            for start in range(0,len(ids),800):
                chunk=ids[start:start+800]
                db.execute('DELETE FROM catalog_works WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk)
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('public_seed_version',version))

    def close(self):
        self._closed=True;self._update_stop.set()
        if self._updater:self._updater.join(timeout=40)
        with self._lock:threads=list(self._inflight.values())
        for thread in threads:thread.join(timeout=9)

    def background(self,key,callback):
        with self._lock:
            if self._closed or key in self._inflight or len(self._inflight)>=4 or self._fail_until.get(key,0)>time.time():return False
            def run():
                try:callback()
                except Exception:
                    with self._lock:self._fail_until[key]=time.time()+30
                finally:
                    with self._lock:self._inflight.pop(key,None)
            thread=threading.Thread(target=run,name='catalog-refresh',daemon=True)
            self._inflight[key]=thread;thread.start();return True

    def seed_query(self,sql,args=()):
        if not self.seed.exists():return []
        with contextlib.closing(sqlite3.connect('file:'+str(self.seed.resolve()).replace('\\','/')+'?mode=ro&immutable=1',uri=True)) as c:
            c.row_factory=sqlite3.Row
            try:return [dict(r) for r in c.execute(sql,args)]
            except sqlite3.OperationalError:return []

    def fetch(self,path,ttl=300,*,binary=False):
        if not path.startswith('/') or path.startswith('//'):raise AppError('INVALID_SOURCE','资源来源不正确')
        key='mikan:'+path
        if not binary:
            cached=self.store.cache_get(key)
            if cached is not None:return cached
        data=self.http.bytes('GET',self.config.mikan_url+path,limit=4*1024*1024,headers={'Accept':'*/*'})
        if binary:return data
        page=data.decode('utf-8','replace')
        if not self._closed:self.store.cache_set(key,page,ttl)
        return page

    @staticmethod
    def current():
        now=datetime.datetime.now(ZoneInfo('Asia/Tokyo'))
        return {'year':now.year,'season':['冬','春','夏','秋'][(now.month-1)//3]}

    def seed_rows(self,where='',args=(),limit=1000):
        rows=self.store.all('SELECT id,payload,year,season,weekday FROM catalog_works '+where+' LIMIT ?',(*args,limit))
        if self.seed.exists():rows+=self.seed_query('SELECT id,payload,year,season,weekday FROM works '+where+' LIMIT ?',(*args,limit))
        result={}
        for row in rows:
            value=json.loads(row['payload']);value.setdefault('id',row['id'])
            if row['season']:value.setdefault('release_season',row['season'])
            if row['year']:value.setdefault('year',row['year'])
            result.setdefault(row['id'],value)
        return list(result.values())[:limit]

    def all_works(self):return self.seed_rows(limit=100000)

    def save_works(self,works):
        with self.store.transaction() as db:
            for work in works:
                bid=str(work.get('mikan_id') or work.get('bangumiId') or '')
                ident=str(work.get('id') or work.get('canonical_id') or ('mikan:'+bid if bid else ''))
                if not ident:continue
                value={**work,'id':ident,'canonical_id':work.get('canonical_id') or ident}
                value.pop('subgroups',None);value.pop('subgroupsCount',None)
                db.execute('INSERT INTO catalog_works VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mikan_id=excluded.mikan_id,title=excluded.title,year=excluded.year,season=excluded.season,weekday=excluded.weekday,payload=excluded.payload,updated_at=excluded.updated_at',
                    (ident,bid,value.get('title') or '',value.get('year'),value.get('release_season'),value.get('air_weekday'),json.dumps(value,ensure_ascii=False),time.time()))
        with self._lock:self._search_index=None

    def sync_result(self,key,details,ttl,failed=False):
        old=self.store.one('SELECT * FROM catalog_sync WHERE key=?',(key,)) or {}
        now=time.time();failures=int(old.get('failures',0))+1 if failed else 0
        with self.store.transaction() as db:
            db.execute('INSERT INTO catalog_sync VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET last_success=excluded.last_success,next_due=excluded.next_due,failures=excluded.failures,details=excluded.details',
                (key,old.get('last_success',0) if failed else now,now+ttl,failures,json.dumps(details,ensure_ascii=False)))

    def start_updates(self):
        if not getattr(self.config,'catalog_auto_update',False) or self._updater:return
        from .catalog_sync import PublicFeeds,update_cross_references
        def run():
            while not self._update_stop.is_set():
                refs=self.store.one('SELECT * FROM catalog_sync WHERE key=?',('references',))
                if not refs or refs['next_due']<=time.time():
                    try:update_cross_references(self,PublicFeeds(self.config.data/'public-feeds'))
                    except Exception:self.sync_result('references',{'status':'retry'},3600,True)
                self.update_next_quarter()
                self.complete_pending_details()
                self._update_stop.wait(60)
        self._updater=threading.Thread(target=run,name='catalog-updates',daemon=True);self._updater.start()

    def complete_pending_details(self,limit=5):
        rows=self.store.all("SELECT payload FROM catalog_works WHERE mikan_id!='' AND (json_extract(payload,'$.provider_ids.bangumi') IS NULL OR json_extract(payload,'$.provider_ids.bangumi')='') ORDER BY updated_at DESC")
        checked=0
        for row in rows:
            if self._update_stop.is_set() or checked>=limit:break
            work=json.loads(row['payload']);bid=work['mikan_id'];key='detail:'+str(bid)
            prior=self.store.one('SELECT * FROM catalog_sync WHERE key=?',(key,))
            if prior and prior['next_due']>time.time():continue
            checked+=1
            try:
                detail=self.mikan_detail(bid);value={**work,**detail,'id':work['id'],'canonical_id':work.get('canonical_id') or work['id']}
                if self._references:value=self._references.enrich(value)
                self.save_works([value]);self.sync_result(key,{'status':'updated'},86400)
            except Exception:self.sync_result(key,{'status':'retry'},3600,True)
        return checked

    def update_next_quarter(self):
        current=self.current();seasons=['冬','春','夏','秋'];pos=seasons.index(current['season'])
        first=(current['year'],current['season']);upcoming=(current['year']+(pos==3),seasons[(pos+1)%4])
        quarters=[first,upcoming]+[(y,s) for y in range(2012,current['year']+1) for s in seasons if (y,s) not in {first,upcoming}]
        for year,season in quarters:
            if self._update_stop.is_set():return None
            key=f'quarter:{year}:{season}';row=self.store.one('SELECT * FROM catalog_sync WHERE key=?',(key,))
            if row and row['next_due']>time.time():continue
            try:
                data=self.load_schedule(year,season);self.store.cache_set(f'schedule:v2:{year}:{season}',data,3600 if (year,season) in {first,upcoming} else 86400)
                self.sync_result(key,{'works':len(data['bangumis'])},3600 if (year,season) in {first,upcoming} else 7*86400)
            except Exception:self.sync_result(key,{'status':'retry'},3600,True)
            return year,season

    def work(self,ident):
        ident=str(ident)
        if ident.startswith('mikan:'):ident=ident.split(':',1)[1]
        rows=self.seed_rows('WHERE id=?',(ident,),1)
        if not rows and ident.isdigit():rows=self.seed_rows('WHERE mikan_id=?',(ident,),1)
        if rows:return self.detail_artwork(self.enrich(self.decorate(rows[0],lookup=False)))
        if ident.isdigit():return self.detail_artwork(self.mikan_detail(ident))
        raise AppError('WORK_NOT_FOUND','作品不在当前目录中',404)

    def seasons(self):
        current=self.current()
        years=list(range(current['year']+(1 if current['season']=='秋' else 0),2011,-1))
        return {'years':years,'seasons':[{'season':s,'label':s+'季'} for s in ('冬','春','夏','秋')],
                'current':current,'source':'mikan115','defaultYear':current['year'],'defaultSeason':current['season']}

    def schedule(self,year=None,season=None):
        current=self.current();year=int(year or current['year']);season=season or current['season']
        if not 1990<=year<=current['year']+2 or season not in {'冬','春','夏','秋'}:raise AppError('INVALID_SEASON','季度参数不正确')
        key=f'schedule:v2:{year}:{season}'
        cached=self.store.cache_get(key)
        if cached is not None:return {**cached,'refreshing':False}
        stale=self.store.cache_get(key,stale=True)
        archived=self.seed_query('SELECT payload FROM catalog_seasons WHERE year=? AND season=?',(year,season))
        if not stale and archived:stale={**json.loads(archived[0]['payload']),'source':'snapshot_fallback'}
        items=self.seed_rows('WHERE year=? AND season=?',(year,season),1500)
        def refresh():
            result=self.load_schedule(year,season)
            if not self._closed:self.store.cache_set(key,result,600 if {'year':year,'season':season}==current else 86400)
        if stale or items:
            self.background(key,refresh)
            if stale:return {**stale,'refreshing':key in self._inflight}
            compact=[self.summary(self.decorate(x,lookup=False)) for x in items]
            return {'days':[{'day':i,'label':['星期日','星期一','星期二','星期三','星期四','星期五','星期六'][i],
                    'items':[x for x in compact if x.get('air_weekday') is not None and str(x['air_weekday'])==str(i)]} for i in range(7)],
                    'bangumis':compact,'unknownItems':[x for x in compact if x.get('air_weekday') is None],
                    'year':year,'season':season,'source':'snapshot_fallback','source_notice':'缓存',
                    'refreshing':key in self._inflight}
        try:
            result=self.load_schedule(year,season)
            self.store.cache_set(key,result,600 if {'year':year,'season':season}==current else 86400)
            return {**result,'refreshing':False}
        except AppError:
            raise ProviderError('SCHEDULE_UNAVAILABLE','加载失败，请重试')

    def load_schedule(self,year,season):
        query=urllib.parse.urlencode({'year':year,'seasonStr':season})
        current=self.current();days=[]
        paths=['/Home/MobileBangumiCoverFlowByDayOfWeek?'+query,
               '/' if {'year':year,'season':season}==current else '/Home/BangumiCoverFlowByDayOfWeek?'+query]
        for path in paths:
            try:
                page=self.fetch(path,600 if {'year':year,'season':season}==current else 86400)
                days=parser._parse_mikan_weekday_schedule(page,self.config.mikan_url)
                if any(g['items'] for g in days):break
            except AppError:continue
        if not any(g['items'] for g in days):raise ProviderError('MIKAN_EMPTY_SCHEDULE','加载失败，请重试')
        ids=list({str(w.get('bangumiId')) for g in days for w in g['items']})
        known={}
        if ids:
            for row in self.seed_rows('WHERE mikan_id IN ('+','.join('?' for _ in ids)+')',ids,3000):
                known.setdefault(str(row.get('mikan_id')),row)
        full=[]
        for group in days:
            values=[self.decorate({**x,'air_weekday':group['day']},known=known.get(str(x.get('bangumiId')),{}),lookup=False) for x in group['items']]
            full.extend(values);group['items']=[self.summary(x) for x in values]
        self.save_works(full)
        return {'days':days,'bangumis':[x for g in days for x in g['items']], 'unknownItems':[],
                'year':year,'season':season,'source':'live','source_notice':''}

    @staticmethod
    def summary(work):
        fields={'id','canonical_id','bangumiId','mikan_id','mikanBangumiId','title','year','image','poster_url','portrait_url',
                'air_weekday','updateDate','ratings','defaultSeason','display_season','genres','media_type','mikan_poster_url'}
        return {k:v for k,v in work.items() if k in fields}

    def decorate(self,item,*,known=None,lookup=True):
        bid=str(item.get('bangumiId') or item.get('mikan_id') or '')
        if known is None:
            rows=self.seed_rows('WHERE mikan_id=?',(bid,),1) if lookup and bid.isdigit() else []
            known=rows[0] if rows else {}
        out={**known,**{k:v for k,v in item.items() if v is not None and v!='' and not (k in {'episodes','seasons'} and not v)}}
        if urllib.parse.urlsplit(str(known.get('poster_url') or '')).hostname=='image.tmdb.org':out['poster_url']=out['image']=known['poster_url']
        out.setdefault('image',out.get('poster_url',''));out.setdefault('poster_url',out.get('image',''))
        if bid.isdigit():
            out.setdefault('mikan_id',bid);out.setdefault('id','mikan:'+bid)
        for value in [item.get('image'),item.get('poster_url'),out.get('image'),out.get('poster_url')]:
            if isinstance(value,str) and urllib.parse.urlsplit(value).path.lower().startswith('/images/bangumi/'):
                out['mikan_poster_url']=value;break
        # Mikan's square thumbnails crop portrait artwork. Preserve the source
        # aspect ratio while retaining its width/format optimization.
        origin=urllib.parse.urlsplit(self.config.mikan_url)
        for key in ('image','poster_url','mikan_poster_url'):
            value=out.get(key)
            if not isinstance(value,str):continue
            url=urllib.parse.urlsplit(html.unescape(value))
            if (not url.netloc or url.netloc==origin.netloc) and url.path.lower().startswith('/images/bangumi/'):
                if not url.netloc:url=urllib.parse.urlsplit(urllib.parse.urljoin(self.config.mikan_url,value))
                query=[(k,v) for k,v in urllib.parse.parse_qsl(url.query,keep_blank_values=True) if k.lower()!='height']
                out[key]=urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query)))
        out.setdefault('canonical_id',out.get('id',''));out['inLibrary']=False;out['playable']=False
        return out

    def enrich(self,work):
        ident=work.get('tmdb_id') or (work.get('provider_ids') or {}).get('tmdb')
        if not str(ident or '').isdigit():return work
        mapping=work.get('tmdb_mapping') or {}
        if mapping.get('status') in {'season_ambiguous','work_only','multiple_works','episode_scope'}:return work
        season=int(work.get('tmdb_season') if work.get('tmdb_season') is not None else work.get('display_season') or work.get('defaultSeason') or 1)
        kind='movie' if work.get('media_type')=='movie' else 'tv'
        key=f'tmdb:v2:{kind}:{ident}:{season}'
        cached=self.store.cache_get(key,stale=True)
        if self.metadata.status()['tmdb_configured'] and self.store.cache_get(key) is None:
            def refresh():
                result=self.metadata.title(kind,ident,season)
                if not self._closed:self.store.cache_set(key,result,86400)
            self.background(key,refresh)
        if not cached:return {**work,'metadata_pending':key in self._inflight}
        out=dict(work)
        for field in ['poster_url','backdrop_url','overview','genres','episodes','seasons','credits']:
            if field=='credits' and not any((cached.get('credits') or {}).values()):continue
            if cached.get(field):out[field]=cached[field]
        if out.get('poster_url'):out['image']=out['poster_url']
        out['ratings']={**(work.get('ratings') or {}),**(cached.get('ratings') or {})}
        out['metadata_pending']=key in self._inflight
        return out

    def detail_artwork(self,work):
        out={**work,'detail_artwork':{'backdrops':[],'logos':[],'logo_language':None}}
        ident=work.get('tmdb_id') or (work.get('provider_ids') or {}).get('tmdb')
        mapping=work.get('tmdb_mapping') or {}
        if not str(ident or '').isdigit() or work.get('tmdb_link_conflict') or mapping.get('status')=='multiple_works':return out
        kind='movie' if mapping.get('type')=='movie' or work.get('media_type')=='movie' else 'tv'
        key=f'tmdb:artwork:v1:{kind}:{ident}'
        cached=self.store.cache_get(key,stale=True)
        if self.metadata.status()['tmdb_configured'] and self.store.cache_get(key) is None:
            def refresh():
                result=self.metadata.artwork(kind,ident)
                if not self._closed:self.store.cache_set(key,result,86400)
            self.background(key,refresh)
        if cached is None:
            seed_key=f'tmdb:artwork:seed:v1:{kind}:{ident}'
            seed=self.store.cache_get(seed_key)
            if seed is None:
                logos=[]
                sources=[work]
                sources.extend(json.loads(r['payload']) for r in self.seed_query("SELECT payload FROM works WHERE CAST(json_extract(payload,'$.tmdb_id') AS TEXT)=? AND json_type(payload,'$.logos')='array' LIMIT 20",(str(ident),)))
                sources.extend(json.loads(r['payload']) for r in self.seed_query("SELECT payload FROM provider_metadata WHERE provider='TMDB_DETAIL' AND external_id=?",(str(ident),)))
                for source in sources:
                    source_kind='movie' if (source.get('tmdb_mapping') or {}).get('type')=='movie' or source.get('media_type')=='movie' else 'tv'
                    if source_kind!=kind or source.get('tmdb_link_conflict'):continue
                    logos.extend((source.get('images') or source).get('logos') or [])
                seed=self.metadata.select_artwork({'logos':logos})
                self.store.cache_set(seed_key,seed,86400)
            cached={**seed,'backdrops':self.metadata.select_artwork({},legacy_backdrop=work.get('backdrop_url'))['backdrops']}
        out['detail_artwork']={**cached,'tmdb_id':int(ident),'media_type':kind}
        out['metadata_pending']=bool(work.get('metadata_pending') or key in self._inflight)
        return out

    def person(self,ident):
        if not str(ident).isdigit():raise AppError('NOT_FOUND','人物不存在',404)
        key='person:catalog:'+str(ident);cached=self.store.cache_get(key)
        if cached is not None:return cached
        result={}
        for group in ['cast','crew']:
            rows=self.seed_query("SELECT j.value FROM works w, json_each(w.payload,'$.credits."+group+"') j WHERE CAST(json_extract(j.value,'$.id') AS TEXT)=? LIMIT 1",(str(ident),))
            if not rows:continue
            value=json.loads(rows[0]['value'])
            result={'id':int(ident),'name':value.get('name') or value.get('original_name') or '',
                    'profile_url':value.get('profile_url') or self.metadata.image(value.get('profile_path'),'w185'),
                    'known_for_department':value.get('department') or ('Acting' if group=='cast' else ''),'biography':''}
            break
        self.store.cache_set(key,result,86400 if result else 600);return result

    def search(self,term):
        term=str(term).strip()[:200]
        if not term:return {'query':'','results':[],'count':0}
        with self._lock:
            if self._search_index is None:self._search_index=fuzzy.build_index(self.all_works())
            index=self._search_index
        items=fuzzy.search(index,term)
        def refresh():
            page=self.fetch('/Home/Search?'+urllib.parse.urlencode({'searchstr':term}),300)
            pattern=r'<a[^>]*href=["\']/Home/Bangumi/(\d+)["\'][^>]*>[\s\S]*?<span[^>]*data-src=["\']([^"\']+)["\'][\s\S]*?<div[^>]*class=["\']an-text["\'][^>]*title=["\']([^"\']+)["\']'
            found=set()
            for match in re.finditer(pattern,page):
                bid,image,title=match.groups()
                if bid in found:continue
                found.add(bid);image=urllib.parse.urljoin(self.config.mikan_url,image)
                self.save_works([self.decorate({'bangumiId':bid,'mikan_id':bid,'title':html.unescape(title),'image':image,'poster_url':image})])
        key='search:'+term
        if self.store.cache_get(key) is None:
            def update():refresh();self.store.cache_set(key,True,600)
            self.background(key,update)
        unique={str(x.get('mikan_id') or x.get('bangumiId') or x.get('id')):self.summary(x) for x in items}
        return {'query':term,'results':list(unique.values()),'count':len(unique),'refreshing':key in self._inflight}

    def mikan_detail(self,bid,subgroup_id=None,expanded=False):
        if not str(bid).isdigit():raise AppError('INVALID_MIKAN_ID','蜜柑作品标识不正确')
        page=self.fetch('/Home/Bangumi/'+str(bid),600)
        title_match=re.search(r'<p[^>]*class=["\']bangumi-title["\'][^>]*>([\s\S]*?)</p>',page)
        title=parser._mikan_plain_text(title_match.group(1)) if title_match else ''
        if not title:raise ProviderError('MIKAN_DETAIL_UNAVAILABLE','作品加载失败，请重试')
        poster=''
        for tag in re.findall(r'<[a-z][^>]*>',page,re.I):
            attributes=parser._mikan_tag_attributes(tag)
            if 'bangumi-poster' not in attributes.get('class','').split():continue
            match=re.search(r'url\(\s*[\"\']?([^\)\"\']+)',attributes.get('style',''),re.I)
            poster=urllib.parse.urljoin(self.config.mikan_url,(match.group(1) if match else attributes.get('data-src') or attributes.get('src') or '').strip())
            if poster:break
        matches=list(re.finditer(r'<div[^>]*class=["\']subgroup-text["\'][^>]*id=["\'](\d+)["\'][^>]*>',page));groups=[]
        for i,m in enumerate(matches):
            sg=m.group(1)
            chunk=page[m.end():matches[i+1].start() if i+1<len(matches) else len(page)]
            header=chunk.split('class="mikan-rss"',1)[0][:1500]
            publisher=re.search(r'<a[^>]*href=["\']/Home/PublishGroup/(\d+)["\'][^>]*>([\s\S]*?)</a>',header)
            group_name=re.search(r'<a[^>]*class=["\'][^"\']*text-muted[^"\']*["\'][^>]*>([^<]+)</a>',header)
            leading=parser._mikan_plain_text(re.split(r'<(?:a|span|div)\b',header,1,flags=re.I)[0]).strip()
            name=parser._mikan_plain_text(publisher[2]) if publisher else leading or (html.unescape(group_name[1]).strip() if group_name else '字幕组 '+sg)
            source=chunk
            if expanded and (not subgroup_id or str(subgroup_id)==sg) and 'js-expand-episode' in chunk:
                source=self.fetch('/Home/ExpandEpisodeTable?'+urllib.parse.urlencode({'bangumiId':bid,'subtitleGroupId':sg,'take':500}),600)
            resources=parser.parse_mikan_table_rows(source)
            for r in resources:
                if r.get('torrentUrl'):r['torrentUrl']=self.config.mikan_url+urllib.parse.urlsplit(r['torrentUrl']).path
                r['subgroupId']=sg;r['subgroupName']=name
                r['release']=parse_release(r['title'],sg,name)
            groups.append({'subgroupId':sg,'subgroupName':name,'publisherId':publisher[1] if publisher else '', 'resources':resources,'resourceCount':len(resources),'status':'normal'})
        with self.store.transaction() as db:
            for group in groups:
                db.execute('INSERT INTO work_groups VALUES(?,?,?,?) ON CONFLICT(bangumi_id,group_id) DO UPDATE SET name=excluded.name,seen_at=excluded.seen_at',(str(bid),group['subgroupId'],group['subgroupName'],time.time()))
                db.execute('DELETE FROM group_publishers WHERE bangumi_id=? AND group_id=?',(str(bid),group['subgroupId']))
                if group['publisherId']:db.execute('INSERT INTO group_publishers VALUES(?,?,?)',(str(bid),group['subgroupId'],group['publisherId']))
                for resource in group['resources']:
                    resource_hash=str(resource.get('resourceHash') or '').lower()
                    if re.fullmatch(r'[a-f0-9]{40}',resource_hash):db.execute('INSERT INTO release_sources VALUES(?,?,?,?) ON CONFLICT(resource_hash,bangumi_id,group_id) DO UPDATE SET seen_at=excluded.seen_at',(resource_hash,str(bid),group['subgroupId'],time.time()))
        provider_ids={}
        match=re.search(r'https?://(?:bgm\.tv|bangumi\.tv|chii\.in)/subject/(\d+)',page)
        if match:provider_ids['bangumi']=match[1]
        result=self.decorate({'bangumiId':str(bid),'mikan_id':str(bid),'mikanBangumiId':str(bid),
                              'title':title,'image':poster,'poster_url':poster,'subgroups':groups,'subgroupsCount':len(groups),'episodes':[],
                              'media_type':'series','source':'MIKAN'})
        result['provider_ids']={**result.get('provider_ids',{}),**provider_ids}
        result.setdefault('defaultSeason',parser.parse_season_and_clean_title(title)[1] or 1)
        return result

    def resources(self,ident,bid=None,subgroup=None):
        work=self.work(ident) if ident else {}
        source_id=str(bid or work.get('mikan_id') or work.get('mikanBangumiId') or '')
        if not source_id and str(ident).isdigit():source_id=str(ident)
        if not source_id:
            return {'work_id':ident,'source_required':True,'title':work.get('title',''),'groups':[]}
        detail=self.mikan_detail(source_id,subgroup,True)
        groups=[g for g in detail['subgroups'] if not subgroup or g['subgroupId']==str(subgroup)]
        return {'work_id':ident or source_id,'bangumi_id':source_id,'title':detail['title'],'source_required':False,'groups':groups}

    def resolve_resource(self,body):
        ident=str(body.get('work_id') or body.get('bangumi_id') or '')
        result=self.resources(ident,body.get('bangumi_id'),body.get('subgroup_id'))
        wanted=str(body.get('resource_hash') or '').lower()
        matches=[r for g in result['groups'] for r in g['resources'] if str(r.get('resourceHash','')).lower()==wanted]
        if len(matches)!=1:raise AppError('RESOURCE_NOT_FOUND','请选择资源',409)
        r=matches[0]
        magnet=r.get('magnetUrl')
        if not magnet:
            url=urllib.parse.urlsplit(r.get('torrentUrl') or '')
            if not url.path.startswith('/Download/') or not url.path.endswith('.torrent'):
                raise AppError('MAGNET_UNAVAILABLE','资源缺少磁力链接',409)
            magnet=torrent_magnet(self.fetch(url.path,binary=True),r['title'])
        return magnet,r['title'],ident

    def rss_bytes(self,bid,sg):
        if not str(bid).isdigit() or not str(sg).isdigit():raise AppError('INVALID_FEED','RSS 来源标识不正确')
        return self.fetch('/RSS/Bangumi?'+urllib.parse.urlencode({'bangumiId':bid,**({'subgroupid':sg} if str(sg)!='0' else {})}),binary=True)
