import concurrent.futures
import contextlib
import gzip
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse

from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.errors import AppError,ProviderError
from mikan115.metadata import Metadata
from mikan115.store import Store
from tests.test_workflow import Harness

KEY='0123456789abcdef0123456789abcdef'
TOKEN='eyJ'+('syntheticReadToken_'*8)+'.signature'

class TMDBHTTP:
    def __init__(self):self.calls=[];self.seasons=[1,2]
    def json(self,method,url,headers=None,**kwargs):
        u=urllib.parse.urlsplit(url);q=urllib.parse.parse_qs(u.query);headers=headers or {}
        self.calls.append((method,u.path,q,headers))
        valid=q.get('api_key')==[KEY] or headers.get('Authorization')=='Bearer '+TOKEN
        if not valid:return {'success':False,'status_message':'invalid '+str(q)}
        if u.path.endswith('/authentication'):return {'success':True}
        if '/episode/' in u.path:
            return {'id':202,'season_number':2,'episode_number':2,'name':'番外：回家'}
        if '/season/' in u.path:
            number=int(u.path.rsplit('/',1)[1]);return {'name':f'第 {number} 季','air_date':f'202{number}-10-01',
                'poster_path':f'/season-{number}.jpg','overview':f'第{number}季简介',
                'episodes':[{'id':number*100,'season_number':number,'episode_number':1,'name':'首集','air_date':f'202{number}-10-01'}]}
        return {'id':77,'name':'测试剧集','poster_path':'/season-1.jpg','backdrop_path':'/backdrop.jpg',
                'first_air_date':'2021-10-01','overview':'第一季简介','vote_average':8.6,'vote_count':100,
                'seasons':[{'season_number':n,'name':f'第 {n} 季'} for n in self.seasons]}

class MetadataContract(unittest.TestCase):
    def test_api_key_and_bearer_authentication_are_encrypted_and_never_returned(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp));http=TMDBHTTP();metadata=Metadata(store,http)
            for credential in [KEY,TOKEN]:
                result=metadata.configure(credential);self.assertTrue(result['tmdb_configured'])
                self.assertNotIn(credential,json.dumps(result));self.assertNotIn(credential,store.path.read_bytes().decode('latin1'))
                self.assertEqual(metadata.credential(),credential)
            self.assertEqual(http.calls[-1][3]['Authorization'],'Bearer '+TOKEN)
            metadata.configure('',clear=True);self.assertFalse(metadata.status()['tmdb_configured'])

    def test_invalid_key_does_not_replace_working_credentials_or_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata=Metadata(Store(Path(temp)),TMDBHTTP());metadata.configure(KEY)
            bad='f'*32
            with self.assertRaises(AppError) as caught:metadata.configure(bad)
            self.assertNotIn(bad,str(caught.exception));self.assertEqual(metadata.credential(),KEY)

    def test_second_season_uses_exact_season_poster_and_episodes(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata=Metadata(Store(Path(temp)),TMDBHTTP());metadata.configure(KEY)
            result=metadata.title('tv',77,2)
            self.assertTrue(result['poster_url'].endswith('/season-2.jpg'))
            self.assertEqual(result['episodes'][0]['season_number'],2)
            self.assertEqual(result['year'],2022)
            self.assertEqual(result['tmdbTitle'],'测试剧集 · 第 2 季')

    def test_absent_second_season_never_borrows_first_season(self):
        with tempfile.TemporaryDirectory() as temp:
            http=TMDBHTTP();http.seasons=[1];metadata=Metadata(Store(Path(temp)),http);metadata.configure(KEY)
            result=metadata.title('tv',77,2)
            self.assertEqual(result['poster_url'],'');self.assertEqual(result['episodes'],[])
            self.assertEqual(result['overview'],'');self.assertIsNone(result['year'])
            self.assertNotIn('tmdbTitle',result)

    def test_no_confirmed_season_only_loads_series_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            http=TMDBHTTP();metadata=Metadata(Store(Path(temp)),http);metadata.configure(KEY)
            result=metadata.title('tv',77)
            self.assertEqual(result['tmdbTitle'],'测试剧集')
            self.assertEqual(result['episodes'],[])
            self.assertFalse(any('/season/' in path for _,path,_,_ in http.calls))

    def test_episode_scope_queries_only_the_explicit_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            http=TMDBHTTP();metadata=Metadata(Store(Path(temp)),http);metadata.configure(KEY)
            result=metadata.title('tv',77,2,episode=2)
            self.assertEqual(result['tmdbTitle'],'测试剧集 · 番外：回家')
            self.assertEqual([episode['episode_number'] for episode in result['episodes']],[2])
            self.assertEqual(http.calls[-1][1],'/3/tv/77/season/2/episode/2')
            with self.assertRaises(ProviderError):metadata.title('tv',77,2,episode=3)

    def test_special_season_zero_is_not_changed_into_first_season(self):
        with tempfile.TemporaryDirectory() as temp:
            http=TMDBHTTP();http.seasons=[0,1];metadata=Metadata(Store(Path(temp)),http);metadata.configure(KEY)
            result=metadata.title('tv',77,0)
            self.assertEqual(result['defaultSeason'],0);self.assertEqual(result['episodes'][0]['season_number'],0)

class CatalogTitles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name);(root/'seed').mkdir()
        with contextlib.closing(sqlite3.connect(root/'seed/catalog.sqlite')) as db:
            db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,mikan_id TEXT,title TEXT,year INTEGER,season TEXT,weekday INTEGER,payload TEXT)')
            db.execute('CREATE TABLE provider_metadata(provider TEXT,external_id TEXT,payload TEXT)')
            payload={'tmdb_id':77,'media_type':'series','title':'数据库剧名',
                     'seasons':[{'season_number':2,'name':'新篇章'}]}
            db.execute('INSERT INTO provider_metadata VALUES(?,?,?)',('TMDB_DETAIL','77',json.dumps(payload)))
            db.commit()
        self.store=Store(root/'state')
        self.catalog=Catalog(self.store,Config(root=root,data=root/'state'))
        def offline(*args,**kwargs):raise ProviderError()
        self.catalog.fetch=offline
        self.work={'id':'second','title':'蜜柑剧名','tmdb_id':77,'year':2026,'release_season':'夏',
                   'defaultSeason':1,'tmdb_mapping':{'type':'tv','id':77,'season':2,'status':'linked'},
                   'episodes':[{'season_number':1,'episode_number':1},{'season_number':2,'episode_number':2}]}

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def test_cached_schedule_detail_and_search_use_the_exact_title(self):
        self.catalog.save_works([self.work])
        old={'id':'second','title':'蜜柑剧名','tmdbTitle':'过期标题'}
        self.store.cache_set('schedule:v2:2026:夏',{'days':[{'day':1,'items':[old]}],'bangumis':[old]})
        for term in ('蜜柑剧名','数据库剧名','新篇章'):
            self.assertEqual(self.catalog.search(term)['results'][0]['tmdbTitle'],'数据库剧名 · 新篇章')
        schedule=self.catalog.schedule(2026,'夏')
        self.assertEqual(schedule['days'][0]['items'][0]['tmdbTitle'],'数据库剧名 · 新篇章')
        self.assertEqual(schedule['bangumis'][0]['title'],'蜜柑剧名')
        result=self.catalog.work('second')
        self.assertEqual(result['tmdbTitle'],'数据库剧名 · 新篇章')
        self.assertEqual(result['defaultSeason'],2)
        self.assertEqual([item['season_number'] for item in result['episodes']],[2])

    def test_no_title_or_metadata_request_for_unresolved_scope(self):
        self.store.set_setting('tmdb_credentials',self.store.encrypt(TOKEN))
        for incomplete in ({'tmdb_mapping':{'status':'season_ambiguous'}},{'tmdb_mapping':{}},
                           {'tmdb_link_conflict':True},{'media_type':'movie','tmdb_mapping':{}}):
            work={**self.work,**incomplete}
            self.assertNotIn('tmdbTitle',self.catalog.summary(work))
            if incomplete.get('media_type')!='movie':
                self.assertNotIn('metadata_pending',self.catalog.enrich(work))
        self.assertFalse(self.catalog._inflight)

    def test_tmdb_lookup_does_not_return_the_first_season_for_every_request(self):
        first={**self.work,'id':'first','tmdb_mapping':{**self.work['tmdb_mapping'],'season':1}}
        self.catalog.save_works([first,self.work])
        self.assertEqual(self.catalog.tmdb_work('tv',77,1)['id'],'first')
        self.assertEqual(self.catalog.tmdb_work('tv',77,2)['id'],'second')
        self.assertIsNone(self.catalog.tmdb_work('tv',77,None))
        self.assertIsNone(self.catalog.tmdb_work('movie',77,None))

    def test_offline_special_title_keeps_its_exact_episode_number(self):
        work={**self.work,'tmdb_mapping':{'type':'tv','id':77,'season':0,'episode':3,'status':'episode_scope'}}
        result=self.catalog.enrich(work)
        self.assertEqual(result['tmdbTitle'],'数据库剧名 · 特别篇第 3 集')
        self.assertEqual(result['defaultSeason'],0)
        self.assertEqual(result['episodes'],[])

    def test_shared_mikan_id_cannot_switch_the_selected_season(self):
        first={**self.work,'id':'first','mikan_id':'123','provider_ids':{'bangumi':'100'},
               'tmdb_mapping':{**self.work['tmdb_mapping'],'season':1}}
        second={**self.work,'mikan_id':'123','provider_ids':{'bangumi':'200'}}
        self.catalog.save_works([first,second])
        self.assertEqual(self.catalog.work('second')['tmdb_season'],2)
        self.assertEqual({row['id'] for row in self.catalog.search('蜜柑剧名')['results']},{'first','second'})
        self.assertIsNone(self.catalog.decorate({'bangumiId':'123','title':'蜜柑剧名'}).get('tmdb_id'))
        self.catalog.fetch=lambda *args,**kwargs:'<p class="bangumi-title">第二季</p><a href="https://bangumi.tv/subject/200">Bangumi</a>'
        self.assertEqual(self.catalog.work('123')['id'],'second')

class FastHistory(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'seed').mkdir()
        self.store=Store(self.root/'state');self.catalog=Catalog(self.store,Config(root=self.root,data=self.root/'state'))
        with contextlib.closing(sqlite3.connect(self.root/'seed/catalog.sqlite',isolation_level=None)) as db:
            db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,mikan_id,title,year,season,weekday,payload)')
            for ident,day in [('one',4),('unknown',None)]:
                work={'id':ident,'title':ident,'year':2026,'air_weekday':day,'episodes':[{'overview':'x'*10000}]}
                db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',(ident,'',ident,2026,'夏',day,json.dumps(work)))
    def tearDown(self):self.catalog.close();self.temp.cleanup()

    def test_cold_history_returns_seed_without_waiting_for_slow_upstream(self):
        entered=threading.Event();release=threading.Event()
        def slow(*a,**kw):entered.set();release.wait(3);raise ProviderError()
        self.catalog.fetch=slow
        start=time.monotonic();result=self.catalog.schedule(2026,'夏');elapsed=time.monotonic()-start
        self.assertLess(elapsed,.5);self.assertEqual(len(result['bangumis']),2)
        self.assertNotIn('episodes',result['bangumis'][0]);self.assertEqual(len(result['unknownItems']),1)
        self.assertTrue(entered.wait(1));release.set()

    def test_parallel_history_requests_share_one_refresh(self):
        release=threading.Event();count=0;lock=threading.Lock()
        def refresh(*a,**kw):
            nonlocal count
            with lock:count+=1
            release.wait(3)
            return {'days':[],'bangumis':[],'year':2026,'season':'夏','source':'live'}
        self.catalog.load_schedule=refresh
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.catalog.schedule(2026,'夏'),range(12)))
        self.assertTrue(all(len(x['bangumis'])==2 for x in results));self.assertEqual(count,1);release.set()

    def test_stale_history_remains_usable_when_refresh_fails(self):
        cached={'days':[],'bangumis':[{'id':'cached','title':'已缓存作品'}],'year':2026,'season':'夏','source':'live'}
        self.store.cache_set('schedule:v2:2026:夏',cached,-1)
        self.catalog.load_schedule=lambda *a:(_ for _ in ()).throw(ProviderError())
        result=self.catalog.schedule(2026,'夏');self.assertEqual(result['bangumis'][0]['id'],'cached')

    def test_second_season_identity_and_three_scores_survive_detail_lookup(self):
        payload={'id':'catalog~2','mikan_id':'4098','bangumiId':'4098','title':'测试作品 第二季','year':2026,
                 'defaultSeason':2,'poster_url':'https://mikanani.me/images/Bangumi/202610/second.jpg?width=400&height=400',
                 'ratings':{'tmdb':8.6,'bangumi':7.4,'mal':8.42}}
        with contextlib.closing(sqlite3.connect(self.root/'seed/catalog.sqlite',isolation_level=None)) as db:
            db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',('catalog~2','4098',payload['title'],2026,'秋',4,json.dumps(payload)))
        result=self.catalog.work('4098')
        self.assertEqual(result['defaultSeason'],2);self.assertEqual(result['year'],2026)
        self.assertEqual(set(result['ratings']),{'tmdb','bangumi','mal'});self.assertNotIn('height=',result['poster_url'])

    def test_cached_cast_details_are_available_without_a_tmdb_key(self):
        payload={'id':'cast-work','title':'测试作品','credits':{'cast':[{'id':123,'name':'测试声优','profile_url':'https://image.tmdb.org/t/p/w185/person.jpg'}]}}
        with contextlib.closing(sqlite3.connect(self.root/'seed/catalog.sqlite',isolation_level=None)) as db:
            db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',('cast-work','','测试作品',2026,'秋',4,json.dumps(payload)))
        result=self.catalog.person('123');self.assertEqual(result['name'],'测试声优')
        self.assertTrue(result['profile_url'].endswith('/person.jpg'));self.assertFalse(self.catalog.metadata.status()['tmdb_configured'])

class MetadataHTTPRoutes(Harness):
    def setUp(self):
        super().setUp();self.app.metadata.http=TMDBHTTP()

    def test_settings_roundtrip_masks_key_and_requires_admin_and_csrf(self):
        status,result,_=self.request('POST','/api/settings/metadata',{'tmdb_credential':KEY})
        self.assertEqual(status,200);self.assertTrue(result['tmdb_configured'])
        status,result,_=self.request('GET','/api/settings/metadata');self.assertNotIn(KEY,json.dumps(result))
        status,_,_=self.request('POST','/api/settings/metadata',{'tmdb_credential':KEY},csrf=False);self.assertEqual(status,403)
        self.request('POST','/api/admin/users',{'username':'ordinary','password':'ordinary-password-123'})
        self.request('POST','/api/auth/login',{'username':'ordinary','password':'ordinary-password-123'})
        status,_,_=self.request('GET','/api/settings/metadata');self.assertEqual(status,403)
        status,_,_=self.request('POST','/api/settings/metadata',{'tmdb_credential':KEY});self.assertEqual(status,403)

    def test_gzip_static_response_and_etag_revalidation_keep_exact_content(self):
        path=self.config.root/'static/assets/example.js';path.parent.mkdir(parents=True,exist_ok=True)
        raw=('const title="测试";\n'*1000).encode();path.write_bytes(raw)
        status,body,headers=self.request('GET','/assets/example.js',headers={'Accept-Encoding':'gzip'},binary=True)
        self.assertEqual(status,200);self.assertEqual(headers['Content-Encoding'],'gzip');self.assertEqual(gzip.decompress(body),raw)
        status,body,_=self.request('GET','/assets/example.js',headers={'If-None-Match':headers['ETag']},binary=True)
        self.assertEqual(status,304);self.assertEqual(body,b'')
        status,body,headers=self.request('GET','/assets/example.js',headers={'Accept-Encoding':'gzip;q=0'},binary=True)
        self.assertEqual(body,raw);self.assertNotIn('Content-Encoding',headers)

    def test_rejected_origin_does_not_poison_the_next_keepalive_request(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST','/api/transfers',body=json.dumps({'magnet':'invalid'}),headers={'Content-Type':'application/json','Origin':'https://another-site.invalid'})
            response=connection.getresponse();self.assertEqual(response.status,403);response.read()
            connection.request('GET','/api/health');response=connection.getresponse()
            self.assertEqual(response.status,200);self.assertTrue(json.loads(response.read())['ok'])
        finally:connection.close()


if __name__=='__main__':unittest.main()
