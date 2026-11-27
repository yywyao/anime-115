import contextlib,json,pathlib,sqlite3,tempfile,time,unittest
from mikan115 import search
from mikan115.catalog import Catalog
from mikan115.catalog_sync import CrossReferences,PublicRedirect
from mikan115.config import Config
from mikan115.errors import ProviderError
from mikan115.store import Store

class SearchRegression(unittest.TestCase):
    def setUp(self):
        self.works=[{'id':'ice','title':'冰之城墙 第二季','aliases':['氷の城壁 第2期','The Fragrant Flower']},{'id':'conan','title':'名侦探柯南','aliases':['名偵探柯南','Detective Conan']},{'id':'other','title':'其他作品'}]
        self.index=search.build_index(self.works)
    def test_partial_chinese_and_missing_particles(self):
        self.assertEqual(search.search(self.index,'冰城墙')[0]['id'],'ice')
    def test_aliases_traditional_chinese_and_case_width(self):
        for q in ['名偵探柯南','ＤＥＴＥＣＴＩＶＥ ＣＯＮＡＮ','detective conan']:
            self.assertEqual(search.search(self.index,q)[0]['id'],'conan')
    def test_single_typo_does_not_change_original_identity(self):
        self.assertEqual(search.search(self.index,'名侦探可南')[0]['id'],'conan');self.assertEqual(self.works[1]['title'],'名侦探柯南')
    def test_empty_or_unrelated_search_has_no_rankings(self):
        self.assertEqual(search.search(self.index,'  '),[]);self.assertEqual(search.search(self.index,'completely unrelated'),[])

class IdentityRegression(unittest.TestCase):
    def make(self,targets,sites=None):
        bg={'items':[{'title':'第二季','titleTranslate':{'zh-Hans':['中文名'],'zh-Hant':['繁體名']},'begin':'2026-10-01','sites':sites or [{'site':'bangumi','id':'200'},{'site':'aniList','id':'100'}]}]}
        return CrossReferences(bg,[],{'subjects':[{'bangumiId':200,'targets':targets}]})
    def test_exact_source_bridge_supplies_season_and_aliases(self):
        result=self.make([{'type':'tv','id':77,'season':2}]).enrich({'id':'original','title':'原名','provider_ids':{'anilist':'100'}})
        self.assertEqual(result['id'],'original');self.assertEqual(result['title'],'原名');self.assertEqual(result['tmdb_season'],2);self.assertEqual(result['tmdb_id'],77)
        self.assertIn('中文名',result['aliases']);self.assertEqual(result['provider_ids']['bangumi'],'200')
    def test_similar_title_without_explicit_source_id_is_never_merged(self):
        result=self.make([{'type':'tv','id':77,'season':2}]).enrich({'id':'other','title':'第二季'})
        self.assertNotIn('tmdb_id',result)
    def test_conflicting_tmdb_target_keeps_existing_link(self):
        result=self.make([{'type':'tv','id':77,'season':2}]).enrich({'id':'original','tmdb_id':88,'provider_ids':{'bangumi':'200'}})
        self.assertEqual(result['tmdb_id'],88);self.assertTrue(result['tmdb_link_conflict'])
    def test_multiple_seasons_are_not_arbitrarily_selected(self):
        result=self.make([{'type':'tv','id':77,'season':1},{'type':'tv','id':77,'season':2}]).enrich({'id':'original','provider_ids':{'bangumi':'200'}})
        self.assertEqual(result['tmdb_mapping']['status'],'season_ambiguous');self.assertNotIn('tmdb_season',result)
    def test_multiple_subject_bridges_do_not_choose_first_bangumi_id(self):
        bg={'items':[{'title':'part','sites':[{'site':'aniList','id':'100'},{'site':'bangumi','id':str(b)}]} for b in [200,201]]}
        index=CrossReferences(bg,[],{'subjects':[]});result=index.enrich({'id':'x','provider_ids':{'anilist':'100'}})
        self.assertNotIn('bangumi',result['provider_ids'])
    def test_film_consensus_does_not_confuse_parent_tv_special(self):
        bg={'items':[{'title':'special','sites':[{'site':'bangumi','id':'200'},{'site':'mal','id':'100'},{'site':'tmdb','id':'movie/335659'}]}]}
        fribb=[{'mal_id':100,'themoviedb_id':{'tv':61422},'season':{'tmdb':0}}]
        index=CrossReferences(bg,fribb,{'subjects':[{'bangumiId':200,'targets':[{'type':'movie','id':335659}]}]})
        result=index.enrich({'id':'x','provider_ids':{'bangumi':'200'}})
        self.assertEqual(result['tmdb_id'],335659);self.assertEqual(result['media_type'],'movie')
    def test_multi_film_subject_is_not_replaced_by_first_movie(self):
        index=self.make([{'type':'movie','id':10},{'type':'movie','id':20}],sites=[{'site':'bangumi','id':'200'},{'site':'tmdb','id':'movie/10'}])
        result=index.enrich({'id':'x','provider_ids':{'bangumi':'200'}})
        self.assertNotIn('tmdb_id',result);self.assertEqual(len(result['tmdb_targets']),2)
    def test_episode_scoped_special_keeps_its_scope(self):
        result=self.make([{'type':'tv','id':77,'season':0,'episode':9}]).enrich({'id':'x','provider_ids':{'bangumi':'200'}})
        self.assertEqual(result['tmdb_id'],77);self.assertEqual(result['tmdb_mapping']['status'],'episode_scope');self.assertEqual(result['tmdb_season'],0)
    def test_corrected_source_identity_drops_stale_original_title_and_score(self):
        index=self.make([{'type':'movie','id':77}]);result=index.enrich({'id':'catalog~999','title':'蜜柑标题','title_original':'错误的特典','ratings':{'mal':8},'provider_ids':{'anilist':'100'}})
        self.assertEqual(result['title_original'],'第二季');self.assertEqual(result['ratings'],{})

class PersistentUpdates(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name);self.store=Store(self.root/'state');self.catalog=Catalog(self.store,Config(root=self.root,data=self.root/'state'))
    def tearDown(self):self.catalog.close();self.temp.cleanup()
    def test_new_work_and_fuzzy_alias_survive_restart(self):
        self.catalog.save_works([{'id':'mikan:99','mikan_id':'99','title':'名侦探柯南','aliases':['Detective Conan']}]);self.catalog.fetch=lambda *a,**kw:(_ for _ in ()).throw(ProviderError())
        self.assertEqual(self.catalog.search('名侦探可南')['results'][0]['mikan_id'],'99')
        self.catalog.close();self.catalog=Catalog(self.store,Config(root=self.root,data=self.root/'state'))
        self.assertEqual(self.catalog.work('99')['title'],'名侦探柯南')
    def test_new_seed_replaces_stale_public_overlay_and_preserves_runtime_only_work(self):
        self.catalog.save_works([{'id':'known','title':'旧缓存'},{'id':'runtime-only','title':'新发现'}])
        (self.root/'seed').mkdir()
        with contextlib.closing(sqlite3.connect(self.root/'seed/catalog.sqlite')) as db:
            db.execute('CREATE TABLE works(id PRIMARY KEY,mikan_id,title,year,season,weekday,payload)')
            db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',('known','','新版目录',2026,'秋',None,json.dumps({'id':'known','title':'新版目录'})));db.commit()
        self.catalog.close();self.catalog=Catalog(self.store,Config(root=self.root,data=self.root/'state'))
        self.assertEqual(self.catalog.work('known')['title'],'新版目录');self.assertEqual(self.catalog.work('runtime-only')['title'],'新发现')
    def test_rollover_prioritizes_current_and_upcoming_without_fixed_year(self):
        self.catalog.current=lambda:{'year':2027,'season':'冬'};calls=[]
        def load(y,s):calls.append((y,s));return {'bangumis':[{'id':'new'}],'days':[]}
        self.catalog.load_schedule=load
        self.assertEqual(self.catalog.update_next_quarter(),(2027,'冬'));self.assertEqual(self.catalog.update_next_quarter(),(2027,'春'))
        self.assertEqual(calls,[(2027,'冬'),(2027,'春')])
    def test_failed_update_keeps_previously_cached_quarter(self):
        old={'days':[],'bangumis':[{'title':'保留作品'}]};self.store.cache_set('schedule:v2:2027:冬',old,999)
        self.catalog.current=lambda:{'year':2027,'season':'冬'};self.catalog.load_schedule=lambda *a:(_ for _ in ()).throw(ProviderError())
        self.catalog.update_next_quarter();self.assertEqual(self.store.cache_get('schedule:v2:2027:冬'),old)
    def test_tmdb_missing_art_keeps_original_mikan_cover(self):
        poster='https://mikanani.me/images/Bangumi/202610/original.jpg'
        work={'id':'x','mikan_id':'99','tmdb_id':77,'defaultSeason':2,'poster_url':poster,'image':poster,'mikan_poster_url':poster}
        self.store.cache_set('tmdb:v2:tv:77:2',{'poster_url':'','backdrop_url':'','ratings':{}},999)
        self.assertEqual(self.catalog.enrich(work)['poster_url'],poster)
    def test_new_mikan_details_are_resolved_by_explicit_bangumi_id(self):
        self.catalog.save_works([{'id':'mikan:99','mikan_id':'99','title':'新作'}])
        self.catalog.mikan_detail=lambda bid:{'mikan_id':bid,'title':'新作','provider_ids':{'bangumi':'200'},'mikan_poster_url':'https://mikanani.me/images/Bangumi/new.jpg'}
        self.catalog._references=CrossReferences({'items':[]},[],{'subjects':[{'bangumiId':200,'targets':[{'type':'tv','id':77,'season':1}]}]})
        self.assertEqual(self.catalog.complete_pending_details(),1);self.assertEqual(self.catalog.work('99')['tmdb_id'],77)

if __name__=='__main__':unittest.main()
