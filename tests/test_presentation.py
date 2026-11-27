import json
from pathlib import Path
import tempfile
import unittest

from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.metadata import Metadata
from mikan115.store import Store


class ArtworkLanguage(unittest.TestCase):
    def test_only_japanese_portrait_poster_is_selected_for_the_exact_season(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata=Metadata(Store(Path(temp)))
            calls=[]
            def call(path,query):
                calls.append((path,query))
                return {'posters':[
                    {'file_path':'/cn.jpg','iso_639_1':'zh','width':680,'height':1020,'vote_average':10},
                    {'file_path':'/wide.jpg','iso_639_1':'ja','width':1920,'height':1080,'vote_average':10},
                    {'file_path':'/japan.jpg','iso_639_1':'ja','width':680,'height':1020},
                ]}
            metadata.call=call
            self.assertEqual(metadata.poster('tv','77',2),'https://image.tmdb.org/t/p/w500/japan.jpg')
            self.assertEqual(calls,[('/tv/77/season/2/images',{'language':'ja'})])

    def test_single_season_episode_label_omits_first_season(self):
        self.assertEqual(Metadata.episode_display_title('剧名 第一季',1,3),'剧名 · 第 3 集')
        self.assertEqual(Metadata.episode_display_title('剧名',2,3),'剧名 · 第 2 季第 3 集')


class LocalizedCatalog(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        (root/'seed').mkdir()
        (root/'seed/bangumi-zh-hans.json').write_text(json.dumps({'100':'名偵探光之美少女！ 第一季'}),encoding='utf-8')
        (root/'seed/tmdb-japanese-posters.json').write_text(json.dumps({'tv:77:1':'/first.jpg'}),encoding='utf-8')
        self.store=Store(root/'state')
        self.catalog=Catalog(self.store,Config(root=root,data=root/'state'))
        self.work={'id':'one','title':'名探偵プリキュア！','overview':'A foreign synopsis.','tmdb_id':77,'tmdb_season':1,
                   'tmdb_mapping':{'status':'linked','type':'tv','id':77,'season':1},'provider_ids':{'bangumi':'100'},
                   'mikan_poster_url':'https://mikanani.me/images/Bangumi/202602/cover.jpg'}

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def test_home_uses_chinese_title_and_season_poster_even_for_old_schedule_cache(self):
        self.catalog.save_works([self.work])
        item={'id':'one','title':'旧标题','poster_url':'https://image.tmdb.org/t/p/w500/old.jpg'}
        self.store.cache_set('schedule:v2:2026:冬',{'bangumis':[item],'days':[]})
        result=self.catalog.schedule(2026,'冬')['bangumis'][0]
        self.assertEqual(result['homeTitle'],'名侦探光之美少女！')
        self.assertEqual(result['homePoster'],'https://image.tmdb.org/t/p/w500/first.jpg')

    def test_sequel_does_not_borrow_first_season_poster_or_lose_its_number(self):
        sequel={**self.work,'title_zh_cn':'名侦探光之美少女！','tmdb_season':2,
                'tmdb_mapping':{**self.work['tmdb_mapping'],'season':2}}
        result=self.catalog.home_presentation(sequel)
        self.assertEqual(result['homeTitle'],'名侦探光之美少女！ · 第 2 季')
        self.assertEqual(result['homePoster'],self.work['mikan_poster_url'])

    def test_detail_accepts_chinese_synopsis_and_suppresses_foreign_one(self):
        wrong={**self.work,'provider_ids':{}}
        self.assertEqual(self.catalog.present_detail(wrong)['overviewZh'],'')
        self.store.cache_set('bangumi:overview-zh:100','在未來小鎮調查神祕事件。')
        self.assertEqual(self.catalog.present_detail(self.work)['overviewZh'],'在未来小镇调查神秘事件。')
        result=self.catalog.present_detail({**self.work,'overview_zh':'在未來小鎮調查神祕事件。'})
        self.assertEqual(result['overviewZh'],'在未来小镇调查神秘事件。')

    def test_chinese_alias_of_same_subject_is_a_fallback_for_a_japanese_title(self):
        work={'title':'サンプル','aliases':['Another title','測試動畫 第一季']}
        self.assertEqual(self.catalog.home_title(work),'测试动画')
