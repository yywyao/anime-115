from mikan115 import releases
from mikan115.errors import AppError
from tests.test_workflow import Harness


class TrackingIdentity(Harness):
    def test_explicit_tmdb_season_selection_persists_exact_ids_and_canonical_title(self):
        self.app.metadata.call=lambda path,query=None:{'id':98765,'name':'作品中文名','original_name':'Original Show','seasons':[
            {'season_number':1,'name':'第一季','episode_count':12},{'season_number':2,'name':'第二季','episode_count':12}],
            'alternative_titles':{'results':[{'title':'Another Confirmed Alias'}]},'external_ids':{'tvdb_id':12345}}
        self.app.metadata.title=lambda kind,ident,season:{'title':'作品中文名','tmdbTitle':'作品中文名 第二季','tmdb_id':98765,
            'known_seasons':[1,2],'episodes':[{'season_number':2,'episode_number':1,'air_date':'2020-01-01'}]}
        seasons=self.app.tracking_catalog.choose({'tmdb_id':98765})
        self.assertEqual([s['season_number'] for s in seasons['seasons']],[1,2])
        self.assertEqual(self.store.all('SELECT * FROM catalog_works'),[])
        result=self.app.tracking_catalog.choose({'tmdb_id':98765,'season':2})
        work=self.catalog.work(result['work']['id'])
        self.assertEqual(self.catalog.tmdb_scope(work),('tv','98765',2,None))
        self.assertEqual(work['provider_ids']['tvdb'],'12345');self.assertEqual(work['series_title'],'作品中文名')
        with self.assertRaises(AppError):self.app.tracking_catalog.choose({'tmdb_id':98765,'season':99})

    def test_explicit_chinese_and_english_seasons_preserve_unknown_part_numbers(self):
        for title in ['Test Show 第二季 - 03 [1080p]','Test Show Season 2 - 03','Test Show 2nd Season - 03','Test Show S02 - 03']:
            parsed=releases.parse(title);self.assertEqual(parsed['season'],2,title);self.assertEqual(parsed['episodes'],[3],title)
        self.assertIsNone(releases.parse('Test Show Part 2 - 03')['season'])
        self.assertIsNone(releases.parse('Test Show II - 03')['season'])
