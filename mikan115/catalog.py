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
from .presentation import chinese, without_first_season
from .public_presentation import PublicOverview
from . import search as fuzzy
from .releases import parse as parse_release


class Catalog:
    def __init__(self,store,config,http=None,metadata=None):
        self.store,self.config,self.http=store,config,http or HTTP(timeout=4)
        self.seed=config.root/'seed'/'catalog.sqlite'
        self.metadata=metadata or Metadata(store)
        self.public_overview=PublicOverview(store,http)
        self._lock=threading.RLock();self._inflight={};self._fail_until={};self._closed=False
        self._search_index=None;self._updater=None;self._update_stop=threading.Event();self._references=None
        self._public_tmdb=None;self._original_titles={};self._live_titles=None
        self._public_logos=None;self._public_season_posters=None
        self._home_names=None;self._home_posters=None
        self._presentation_updater=None
        self._presentation_archive_cursor=0
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
        if self._presentation_updater:self._presentation_updater.join(timeout=9)
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

    def pending_refresh(self,key,callback):
        if self.background(key,callback):return True
        with self._lock:
            return key in self._inflight or (not self._closed and len(self._inflight)>=4
                                            and self._fail_until.get(key,0)<=time.time())

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
        self.start_presentation_updates()
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

    def start_presentation_updates(self):
        if self._presentation_updater:return
        def run():
            while not self._update_stop.is_set():
                try:self.update_presentation_batch()
                except (AppError,sqlite3.Error):pass
                self._update_stop.wait(45)
        self._presentation_updater=threading.Thread(target=run,name='catalog-presentation',daemon=True)
        self._presentation_updater.start()

    def update_presentation_batch(self,limit=4):
        """Warm release material without requiring a visit to each detail page."""
        if self._closed:return 0
        now=time.time()
        current=self.current()
        quarters=('冬','春','夏','秋')
        current_order=current['year']*4+quarters.index(current['season'])
        def priority(work):
            year=work.get('year')
            season=work.get('release_season')
            order=(int(year)*4+quarters.index(season)) if str(year).isdigit() and season in quarters else 0
            return (0 if order in (current_order,current_order+1) else 1,abs(current_order-order))
        fresh={row['key'] for row in self.store.all(
            "SELECT key FROM catalog_cache WHERE expires_at>? AND (key LIKE 'tmdb:%' OR (key LIKE 'bangumi:%' AND payload<>'\"\"'))",(now,))}
        tmdb=self.metadata.status()['tmdb_configured']
        ordered=sorted(self.all_works(),key=priority)
        recent=[work for work in ordered if priority(work)[0]==0]
        archive=[work for work in ordered if priority(work)[0]==1]
        cursor=self._presentation_archive_cursor%len(archive) if archive else 0
        rotated=archive[cursor:]+archive[:cursor]
        posters=[]
        selected=0
        recent_selected=0
        for index,work in enumerate(recent+rotated):
            if self._closed or selected>=limit:break
            is_recent=index<len(recent)
            if archive and is_recent and recent_selected>=(limit+1)//2:continue
            if not is_recent:self._presentation_archive_cursor=(cursor+index-len(recent)+1)%len(archive)
            identity=self.tmdb_artwork_identity(work) if tmdb else None
            scope=self.tmdb_scope(work) if tmdb else None
            bid=self.public_overview.valid_id((work.get('provider_ids') or {}).get('bangumi'))
            work_overview=self.local_overview(work)
            known_posters={url for url in (work.get('mikan_poster_url'),work.get('poster_url') or work.get('image')) if url}
            want_poster=len(known_posters)<2
            queue_public=bid and (not work_overview or want_poster) and self.public_overview.retry_key(bid) not in fresh
            if queue_public:
                queue_public=((not work_overview and self.public_overview.cache_key(bid) not in fresh)
                              or (want_poster and self.public_overview.poster_key(bid) not in fresh))
            queue_title=scope and self.tmdb_cache_key('tmdb:v3',scope) not in fresh
            queue_art=identity and self.tmdb_cache_key('tmdb:artwork:v1',identity) not in fresh
            queue_poster=scope and self.tmdb_cache_key('tmdb:poster-ja:v1',scope[:3]) not in fresh
            if not (queue_public or queue_title or queue_art or queue_poster):continue
            selected+=1
            if is_recent:recent_selected+=1
            if queue_art:self.queue_tmdb_artwork(identity)
            if queue_title:self.queue_tmdb_title(scope)
            if queue_public:self.queue_public_presentation(bid,need_overview=not work_overview,need_poster=want_poster)
            if queue_poster:posters.append(work)
        if posters:self.refresh_home_posters(posters,'background','all')
        return selected

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
        rows=self.seed_rows('WHERE id=?',(ident,),1)
        if rows:return self.present_detail(self.decorate(rows[0],lookup=False))
        if ident.startswith('mikan:'):ident=ident.split(':',1)[1]
        if ident.isdigit():
            known=self.mikan_work(self.seed_rows('WHERE mikan_id=?',(ident,)))
            if known:return self.present_detail(self.decorate(known,lookup=False))
            work=self.mikan_detail(ident)
            if self._references:work=self._references.enrich(work)
            return self.present_detail(work)
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
        if cached is not None:return self.schedule_titles({**cached,'refreshing':False})
        stale=self.store.cache_get(key,stale=True)
        archived=self.seed_query('SELECT payload FROM catalog_seasons WHERE year=? AND season=?',(year,season))
        if not stale and archived:stale={**json.loads(archived[0]['payload']),'source':'snapshot_fallback'}
        items=self.seed_rows('WHERE year=? AND season=?',(year,season),1500)
        def refresh():
            result=self.load_schedule(year,season)
            if not self._closed:self.store.cache_set(key,result,600 if {'year':year,'season':season}==current else 86400)
        if stale or items:
            self.background(key,refresh)
            if stale:return self.schedule_titles({**stale,'refreshing':key in self._inflight})
            compact=[self.summary(self.decorate(x,lookup=False)) for x in items]
            result={'days':[{'day':i,'label':['星期日','星期一','星期二','星期三','星期四','星期五','星期六'][i],
                    'items':[x for x in compact if x.get('air_weekday') is not None and str(x['air_weekday'])==str(i)]} for i in range(7)],
                    'bangumis':compact,'unknownItems':[x for x in compact if x.get('air_weekday') is None],
                    'year':year,'season':season,'source':'snapshot_fallback','source_notice':'缓存',
                    'refreshing':key in self._inflight}
            return self.schedule_titles(result,items)
        try:
            result=self.load_schedule(year,season)
            self.store.cache_set(key,result,600 if {'year':year,'season':season}==current else 86400)
            return self.schedule_titles({**result,'refreshing':False})
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
        by_source={}
        if ids:
            for row in self.seed_rows('WHERE mikan_id IN ('+','.join('?' for _ in ids)+')',ids,3000):
                by_source.setdefault(str(row.get('mikan_id')),[]).append(row)
        known={bid:self.mikan_work(rows) for bid,rows in by_source.items()}
        full=[]
        for group in days:
            values=[self.decorate({**x,'air_weekday':group['day']},known=known.get(str(x.get('bangumiId')),{}),lookup=False) for x in group['items']]
            full.extend(values);group['items']=[self.summary(x) for x in values]
        self.save_works(full)
        return {'days':days,'bangumis':[x for g in days for x in g['items']], 'unknownItems':[],
                'year':year,'season':season,'source':'live','source_notice':''}

    def schedule_titles(self,data,works=None):
        compact=[*data.get('bangumis',[]),*data.get('unknownItems',[])]
        compact.extend(item for group in data.get('days',[]) for item in group['items'])
        ids=list(dict.fromkeys(item['id'] for item in compact if item.get('id')))
        if works is None:
            works=[]
            for i in range(0,len(ids),500):
                batch=ids[i:i+500]
                works.extend(self.seed_rows('WHERE id IN ('+','.join('?' for _ in batch)+')',batch,len(batch)))
        bids={self.public_overview.valid_id((work.get('provider_ids') or {}).get('bangumi')) for work in works}
        public_covers=self.public_overview.cached_posters(bids)
        titles={work['id']:{**self.home_presentation(work,public_covers),'tmdbTitle':self.tmdb_display(work)} for work in works}
        pending=self.refresh_home_posters(works,data.get('year'),data.get('season'))
        requested=set()
        for work in works:
            if len(requested)>=2:break
            if titles[work['id']]['homePosterFallback']:continue
            bid=self.public_overview.valid_id((work.get('provider_ids') or {}).get('bangumi'))
            if bid and bid not in requested:
                if self.queue_public_presentation(bid,need_overview=False,need_poster=True):
                    requested.add(bid)
                    pending=True
        def overlay(item):
            fields=titles.get(item.get('id'))
            return {**item,**(fields if fields is not None else self.home_presentation(item,public_covers))}
        return {**data,'refreshing':bool(data.get('refreshing') or pending),
                'days':[{**group,'items':[overlay(item) for item in group['items']]} for group in data.get('days',[])],
                'bangumis':[overlay(item) for item in data.get('bangumis',[])],
                'unknownItems':[overlay(item) for item in data.get('unknownItems',[])]}

    def presentation_data(self):
        with self._lock:
            if self._home_names is None:
                names=self.seed.with_name('bangumi-zh-hans.json')
                self._home_names=json.loads(names.read_text(encoding='utf-8')) if names.is_file() else {}
                for row in self.seed_query("SELECT external_id,payload FROM provider_metadata WHERE provider='BANGUMI'"):
                    text=json.loads(row['payload']).get('name_cn')
                    if text:self._home_names.setdefault(row['external_id'],text)
            if self._home_posters is None:
                rows=self.store.all("SELECT key,payload,expires_at FROM catalog_cache WHERE key LIKE 'tmdb:poster-ja:v1:%'")
                self._home_posters={row['key']:(json.loads(row['payload']),row['expires_at']) for row in rows}
                seed_posters=self.seed.with_name('tmdb-japanese-posters.json')
                self._public_posters=json.loads(seed_posters.read_text(encoding='utf-8')) if seed_posters.is_file() else {}

    def home_title(self,work):
        self.presentation_data()
        bangumi=str((work.get('provider_ids') or {}).get('bangumi') or '')
        raw=work.get('title') or ''
        aliases=work.get('aliases') or []
        names=(work.get('title_zh_cn'),self._home_names.get(bangumi),work.get('title_zh'),raw,
               *(aliases if isinstance(aliases,list) else []))
        title=next((value for name in names if (value:=chinese(name))),raw)
        scope=self.tmdb_scope(work)
        season=scope[2] if scope else None
        if work.get('media_type')!='movie' and season in (None,1):
            title=without_first_season(title)
        elif type(season) is int and season>1 and title and not re.search(
                r'第\s*[一二三四五六七八九十\d]+\s*[季期]|\b(?:season|s)\s*\d+|\b\d+(?:st|nd|rd|th)\s+season',title,re.I):
            title=self.metadata.display_title(title,season)
        return title

    def home_presentation(self,work,public_covers=None):
        title=self.home_title(work)
        scope=self.tmdb_scope(work)
        poster=''
        if scope:
            key=self.tmdb_cache_key('tmdb:poster-ja:v1',scope[:3])
            with self._lock:
                cached=self._home_posters.get(key,('',0))[0]
            url=cached or self._public_posters.get(self.tmdb_cache_key('',scope[:3]).lstrip(':'),'')
            poster=self.metadata.artwork_url(url,'w500')
        # These are existing poster fields, never a banner, backdrop, still or title logo.
        candidates=[poster,work.get('mikan_poster_url')]
        original=work.get('poster_url') or work.get('image')
        if original and not work.get('tmdb_link_conflict') and original!=work.get('backdrop_url'):
            candidates.append(original)
        if scope:
            kind,ident,season,_=scope
            public=self.public_tmdb_data(kind,ident)
            if season is not None:
                candidates.append(self.metadata.artwork_url(self._public_season_posters.get(f'{ident}:{season}'),'w500'))
            if kind=='movie' or season==1:
                candidates.append(self.metadata.artwork_url(public.get('poster_url') or public.get('poster_path'),'w500'))
        if len({url for url in candidates if url})<2:
            bid=self.public_overview.valid_id((work.get('provider_ids') or {}).get('bangumi'))
            if bid:
                candidates.append(self.public_overview.cached_poster(bid) if public_covers is None else public_covers.get(bid))
        candidates=list(dict.fromkeys(url for url in candidates if url))
        poster,backup=(candidates+['',''])[:2]
        return {'homeTitle':title,'homePoster':poster,'homePosterFallback':backup}

    def refresh_home_posters(self,works,year,season):
        if not works or not self.metadata.status()['tmdb_configured']:return False
        self.presentation_data()
        scopes={}
        for work in works:
            scope=self.tmdb_scope(work)
            if not scope:continue
            key=self.tmdb_cache_key('tmdb:poster-ja:v1',scope[:3])
            if self._home_posters.get(key,('',0))[1]<=time.time():scopes.setdefault(key,scope[:3])
            if len(scopes)>=6:break
        if not scopes:return False
        refresh_key=f'home-posters:{year}:{season}'
        def refresh():
            for key,scope in scopes.items():
                if self._closed:return
                previous=self._home_posters.get(key,('',0))[0]
                try:url=self.metadata.poster(*scope) or previous
                except AppError:
                    # Keep a previously verified image when TMDB is unavailable.
                    url=previous
                    ttl=120
                else:ttl=86400 if url else 1800
                self.store.cache_set(key,url,ttl)
                with self._lock:self._home_posters[key]=(url,time.time()+ttl)
        self.background(refresh_key,refresh)
        return refresh_key in self._inflight

    def summary(self,work):
        fields={'id','canonical_id','bangumiId','mikan_id','mikanBangumiId','title','year','image','poster_url','portrait_url',
                'air_weekday','updateDate','ratings','defaultSeason','display_season','genres','media_type','mikan_poster_url'}
        out={k:v for k,v in work.items() if k in fields}
        title=self.tmdb_display(work)
        if title:out['tmdbTitle']=title
        return out

    @staticmethod
    def local_overview(work):
        return next((value for text in (work.get('overview_zh'),work.get('overview'))
                     if (value:=chinese(text,overview=True))),'')

    def queue_public_presentation(self,bid,*,need_overview,need_poster):
        key='bangumi:presentation:'+bid
        if self.public_overview.should_refresh(bid,need_overview=need_overview,need_poster=need_poster):
            return self.pending_refresh(key,lambda:self.public_overview.refresh(
                bid,need_overview=need_overview,need_poster=need_poster))
        with self._lock:return key in self._inflight

    def present_detail(self,work):
        out=self.detail_artwork(self.enrich(work))
        out['homeTitle']=self.home_title(out)
        overview=chinese(out.get('overview_zh'),overview=True) or self.local_overview(work) or self.local_overview(out)
        scope=self.tmdb_scope(work)
        if not overview and scope:
            kind,ident,season,episode=scope
            public=self.public_tmdb_data(kind,ident)
            if kind=='movie' or (season==1 and episode is None):
                overview=self.local_overview(public)
            if episode is None and season is not None:
                for row in public.get('seasons') or []:
                    if row.get('season_number')==season:
                        overview=self.local_overview(row) or overview
                        break
        bid=self.public_overview.valid_id((work.get('provider_ids') or {}).get('bangumi'))
        if not overview and bid:
            pending=self.queue_public_presentation(bid,need_overview=True,need_poster=False)
            overview=chinese(self.public_overview.cached(bid),overview=True)
            out['metadata_pending']=bool(out.get('metadata_pending') or pending)
        out['overviewZh']=overview
        return out

    def decorate(self,item,*,known=None,lookup=True):
        bid=str(item.get('bangumiId') or item.get('mikan_id') or '')
        if known is None:
            rows=self.seed_rows('WHERE mikan_id=?',(bid,)) if lookup and bid.isdigit() else []
            known=self.mikan_work(rows)
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

    @staticmethod
    def tmdb_scope(work):
        provider_id=(work.get('provider_ids') or {}).get('tmdb')
        ident=work.get('tmdb_id') or provider_id
        mapping=work.get('tmdb_mapping') or {}
        status=mapping.get('status')
        if not str(ident or '').isdigit() or work.get('tmdb_link_conflict'):return None
        if any(value is not None and str(value)!=str(ident) for value in (mapping.get('id'),provider_id)):return None
        if status not in {None,'linked','episode_scope'}:return None
        kind=mapping.get('type') or ('movie' if work.get('media_type')=='movie' else 'tv')
        if kind=='movie' and status!='episode_scope':return kind,str(ident),None,None
        if kind!='tv':return None
        season=mapping.get('season',work.get('tmdb_season'))
        if type(season) is not int or season<0:return None
        episode=mapping.get('episode') if status=='episode_scope' else None
        if status=='episode_scope' and (type(episode) is not int or episode<1):return None
        return kind,str(ident),season,episode

    def mikan_work(self,rows,bangumi_id=None):
        if bangumi_id:
            rows=[row for row in rows if str((row.get('provider_ids') or {}).get('bangumi'))==bangumi_id]
        if len(rows)==1:return rows[0]
        # A shared Mikan ID does not identify a season or a film.
        scopes={(self.tmdb_scope(row),row.get('tmdb_id'),row.get('tmdb_season')) for row in rows}
        subjects={str((row.get('provider_ids') or {}).get('bangumi') or '') for row in rows}
        if len(scopes)==len(subjects)==1 and '' not in subjects:
            return rows[0]
        return {}

    @staticmethod
    def tmdb_cache_key(prefix,scope):
        return prefix+':'+':'.join(str(part) for part in scope if part is not None)

    def tmdb_work(self,kind,ident,season):
        rows=self.seed_rows("WHERE CAST(json_extract(payload,'$.tmdb_id') AS TEXT)=?",(str(ident),))
        works={}
        for row in rows:
            scope=self.tmdb_scope(row)
            if scope and scope[:2]==(kind,str(ident)) and scope[3] is None and (season is None or scope[2]==season):
                works.setdefault(scope,row)
        return self.enrich(next(iter(works.values()))) if len(works)==1 else None

    def public_tmdb_data(self,kind,ident):
        with self._lock:
            if self._public_tmdb is None:
                self._public_tmdb={}
                original_titles=self.seed.with_name('tmdb-original-titles.json')
                if original_titles.is_file():
                    self._original_titles=json.loads(original_titles.read_text(encoding='utf-8'))
                for row in self.seed_query("SELECT external_id,payload FROM provider_metadata WHERE provider IN ('TMDB','TMDB_DETAIL') ORDER BY provider"):
                    data=json.loads(row['payload'])
                    if str(data.get('tmdb_id') or data.get('id'))!=row['external_id']:continue
                    media=data.get('media_type') or ('tv' if data.get('name') or data.get('first_air_date') else 'movie' if data.get('release_date') else '')
                    if media=='series':media='tv'
                    if media in {'tv','movie'}:
                        self._public_tmdb.setdefault((media,row['external_id']),{}).update(data)
                logos=self.seed.with_name('tmdb-japanese-logos.json')
                self._public_logos=json.loads(logos.read_text(encoding='utf-8')) if logos.is_file() else {}
                self._public_season_posters={}
                for row in self.seed_query('SELECT cache_key,payload FROM season_artwork'):
                    data=json.loads(row['payload'])
                    if data.get('status')=='RESOLVED' and row['cache_key'].endswith(':'+str(data.get('season_number'))):
                        self._public_season_posters[row['cache_key']]=data.get('season_poster_url')
            return self._public_tmdb.get((kind,ident),{})

    def tmdb_display(self,work):
        scope=self.tmdb_scope(work)
        if not scope:return ''
        kind,ident,season,episode=scope
        data=self.public_tmdb_data(kind,ident)
        with self._lock:
            if self._live_titles is None:
                rows=self.store.all("SELECT key,payload FROM catalog_cache WHERE key LIKE 'tmdb:display:v1:%'")
                self._live_titles={row['key']:json.loads(row['payload']) for row in rows}
            live=self._live_titles.get(self.tmdb_cache_key('tmdb:display:v1',scope))
        if live:return without_first_season(live) if season==1 else live
        series=(data.get('name') or data.get('title') or data.get('original_name') or data.get('original_title')
                or self._original_titles.get(kind,{}).get(ident,''))
        if not series:return ''
        if episode is not None:
            item=next((item for item in data.get('episodes') or [] if item.get('season_number')==season and
                       item.get('episode_number')==episode),{})
            return Metadata.episode_display_title(series,season,episode,item.get('name') or item.get('title'))
        season_title=next((s.get('name') for s in data.get('seasons') or [] if s.get('season_number')==season),'')
        return Metadata.display_title(series,season,season_title)

    def queue_tmdb_title(self,scope,cached=None):
        kind,ident,season,episode=scope
        key=self.tmdb_cache_key('tmdb:v3',scope)
        if self.store.cache_get(key) is not None:return False
        if cached is None:cached=self.store.cache_get(key,stale=True)
        def refresh():
            try:result=self.metadata.title(kind,ident,season,episode=episode) if episode is not None else self.metadata.title(kind,ident,season)
            except AppError:
                if not self._closed:self.store.cache_set(key,cached or {},300)
                return
            if self._closed:return
            if not result.get('overview_zh') and cached and cached.get('overview_zh'):
                result['overview_zh']=cached['overview_zh']
            if result.get('tmdbTitle'):
                label_key=self.tmdb_cache_key('tmdb:display:v1',scope)
                self.store.cache_set(label_key,result['tmdbTitle'],86400)
                with self._lock:
                    if self._live_titles is not None:self._live_titles[label_key]=result['tmdbTitle']
                    self._search_index=None
            ttl=86400 if result.get('overview_zh') else 3600
            self.store.cache_set(key,result,ttl)
        return self.pending_refresh(key,refresh)

    def enrich(self,work):
        scope=self.tmdb_scope(work)
        out={k:v for k,v in work.items() if k!='tmdbTitle'}
        if not scope:return out
        kind,ident,season,episode=scope
        key=self.tmdb_cache_key('tmdb:v3',scope)
        title=self.tmdb_display(work)
        if title:out['tmdbTitle']=title
        if season is not None:
            out['defaultSeason']=season
            out['tmdb_season']=season
            out['seasons']=[item for item in work.get('seasons') or [] if item.get('season_number')==season and episode is None]
            out['episodes']=[item for item in work.get('episodes') or [] if item.get('season_number')==season and
                             (episode is None or item.get('episode_number')==episode)]
        cached=self.store.cache_get(key,stale=True)
        if cached is None and episode is None:
            old=self.store.cache_get(f'tmdb:v2:{kind}:{ident}:{season if season is not None else 1}',stale=True)
            if old and str(old.get('tmdb_id'))==ident and (kind=='movie' or old.get('defaultSeason')==season):
                name=next((s.get('name') for s in old.get('seasons') or [] if s.get('season_number')==season),'')
                cached={**old,'tmdbTitle':Metadata.display_title(old.get('title'),season,name)}
                if kind=='tv':cached.pop('ratings',None)
        pending=self.metadata.status()['tmdb_configured'] and self.queue_tmdb_title(scope,cached)
        if not cached:return {**out,'metadata_pending':pending}
        if cached.get('tmdbTitle'):out['tmdbTitle']=cached['tmdbTitle']
        for field in ['poster_url','backdrop_url','overview','overview_zh','genres','episodes','seasons','credits']:
            if field=='credits' and not any((cached.get('credits') or {}).values()):continue
            if cached.get(field):out[field]=cached[field]
        if out.get('poster_url'):out['image']=out['poster_url']
        out['ratings']={**(work.get('ratings') or {}),**(cached.get('ratings') or {})}
        out['metadata_pending']=pending
        return out

    @staticmethod
    def tmdb_artwork_identity(work):
        provider=(work.get('provider_ids') or {}).get('tmdb')
        ident=work.get('tmdb_id') or provider
        mapping=work.get('tmdb_mapping') or {}
        if not str(ident or '').isdigit() or work.get('tmdb_link_conflict') or mapping.get('status')=='multiple_works':return None
        if any(value is not None and str(value)!=str(ident) for value in (provider,mapping.get('id'))):return None
        kind=mapping.get('type') or ('movie' if work.get('media_type')=='movie' else 'tv')
        return (kind,str(ident)) if kind in {'movie','tv'} else None

    def queue_tmdb_artwork(self,identity,cached=None):
        kind,ident=identity
        key=self.tmdb_cache_key('tmdb:artwork:v1',identity)
        if self.store.cache_get(key) is not None:return False
        if cached is None:cached=self.store.cache_get(key,stale=True) or {}
        def refresh():
            try:result=self.metadata.artwork(kind,ident)
            except AppError:
                if not self._closed:self.store.cache_set(key,cached,300)
                return
            if self._closed:return
            if not result.get('backdrops'):result['backdrops']=cached.get('backdrops',[])
            if not result.get('logos') and cached.get('logo_language')=='ja':
                result['logos']=cached.get('logos',[])
                result['logo_language']='ja' if result['logos'] else None
            ttl=86400 if result.get('logos') and result.get('backdrops') else 1800
            self.store.cache_set(key,result,ttl)
        return self.pending_refresh(key,refresh)

    def detail_artwork(self,work):
        out={**work,'detail_artwork':{'backdrops':[],'logos':[],'logo_language':None}}
        identity=self.tmdb_artwork_identity(work)
        if not identity:return out
        kind,ident=identity
        public=self.public_tmdb_data(kind,ident)
        key=f'tmdb:artwork:v1:{kind}:{ident}'
        cached=self.store.cache_get(key,stale=True) or {}
        pending=self.metadata.status()['tmdb_configured'] and self.queue_tmdb_artwork(identity,cached)
        logos=cached.get('logos',[]) if cached.get('logo_language')=='ja' else []
        if not logos:
            seed_key=f'tmdb:artwork:seed:v2:{kind}:{ident}'
            seed=self.store.cache_get(seed_key)
            if seed is None:
                candidates=[{'language':'ja','url':url} for url in self._public_logos.get(f'{kind}:{ident}',[])]
                sources=[work]
                sources.extend(json.loads(r['payload']) for r in self.seed_query("SELECT payload FROM works WHERE CAST(json_extract(payload,'$.tmdb_id') AS TEXT)=? AND json_type(payload,'$.logos')='array' LIMIT 20",(str(ident),)))
                sources.extend(json.loads(r['payload']) for r in self.seed_query("SELECT payload FROM provider_metadata WHERE provider='TMDB_DETAIL' AND external_id=?",(str(ident),)))
                for source in sources:
                    source_kind='movie' if (source.get('tmdb_mapping') or {}).get('type')=='movie' or source.get('media_type')=='movie' else 'tv'
                    if source_kind!=kind or source.get('tmdb_link_conflict'):continue
                    candidates.extend((source.get('images') or source).get('logos') or [])
                seed=self.metadata.select_artwork({'logos':candidates})
                self.store.cache_set(seed_key,seed,86400)
            logos=seed.get('logos',[]) if seed.get('logo_language')=='ja' else []
        blocked=set(cached.get('excluded_backdrops') or [])
        backdrops=[url for url in cached.get('backdrops',[]) if url not in blocked]
        if not backdrops:
            legacy=[self.metadata.artwork_url(url,'w1280') for url in (work.get('backdrop_url'),public.get('backdrop_url'))]
            backdrops=list(dict.fromkeys(url for url in legacy if url and url not in blocked))
        out['detail_artwork']={'backdrops':backdrops,'logos':logos,'logo_language':'ja' if logos else None,
                               'tmdb_id':int(ident),'media_type':kind}
        out['metadata_pending']=bool(work.get('metadata_pending') or pending)
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
            if self._search_index is None:
                self._search_index=fuzzy.build_index(({**row,'tmdbTitle':self.tmdb_display(row)} for row in self.all_works()))
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
        unique={}
        for row in items:
            ident=str(row.get('canonical_id') or row.get('id') or row.get('mikan_id'))
            if ident not in unique:unique[ident]=self.summary(row)
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
        known=self.mikan_work(self.seed_rows('WHERE mikan_id=?',(str(bid),)),provider_ids.get('bangumi'))
        result=self.decorate({'bangumiId':str(bid),'mikan_id':str(bid),'mikanBangumiId':str(bid),
                              'title':title,'image':poster,'poster_url':poster,'subgroups':groups,'subgroupsCount':len(groups),'episodes':[],
                              'source':'MIKAN'},known=known)
        result['provider_ids']={**result.get('provider_ids',{}),**provider_ids}
        result.setdefault('media_type','series')
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
