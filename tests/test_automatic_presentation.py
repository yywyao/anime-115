import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.metadata import Metadata
from mikan115.store import Store


class BangumiHTTP:
    def __init__(self):
        self.lookups=[]

    def json(self,method,url,**kwargs):
        self.lookups.append(url)
        ident=int(url.rsplit('/',1)[1])
        return {'id':ident,'type':2,'summary':'主角在小镇认识了新的朋友。','images':{
            'large':f'https://lain.bgm.tv/pic/cover/l/00/12/{ident}_cover.jpg'}}


class AutomaticPresentation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        (root/'seed').mkdir()
        self.store=Store(root/'state')
        self.http=BangumiHTTP()
        self.catalog=Catalog(self.store,Config(root=root,data=root/'state'),self.http)
        self.catalog.current=lambda:{'year':2026,'season':'秋'}
        def synchronous(key,callback):
            callback()
            return True
        self.catalog.background=synchronous
        self.new={
            'id':'next-season','title':'新番','provider_ids':{'bangumi':'200','tmdb':'77'},'year':2027,'release_season':'冬',
            'media_type':'series','tmdb_id':77,'tmdb_season':1,'tmdb_mapping':{'id':77,'type':'tv','season':1,'status':'linked'},
        }

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def test_next_quarter_warms_public_assets_before_the_first_detail_visit(self):
        old={**self.new,'id':'old-season','year':2017,'provider_ids':{'bangumi':'201'}}
        self.catalog.save_works([old,self.new])
        self.assertEqual(self.catalog.update_presentation_batch(limit=1),1)
        self.assertEqual(len(self.http.lookups),1)
        self.assertTrue(self.http.lookups[0].endswith('/200'))
        self.assertEqual(self.catalog.home_presentation(self.new)['homePoster'],'https://lain.bgm.tv/pic/cover/l/00/12/200_cover.jpg')
        self.assertEqual(self.catalog.work('next-season')['overviewZh'],'主角在小镇认识了新的朋友。')
        self.assertEqual(len(self.http.lookups),1)

    def test_configured_tmdb_warms_all_exact_assets_without_opening_a_page(self):
        self.catalog.save_works([self.new])
        metadata=self.catalog.metadata
        metadata.status=lambda:{'tmdb_configured':True}
        metadata.title=lambda *_:{'overview_zh':'这是提供方的中文简介。'}
        metadata.artwork=lambda *_:{'backdrops':['https://image.tmdb.org/t/p/w1280/wide.jpg'],
                                     'logos':['https://image.tmdb.org/t/p/original/jp.png'],'logo_language':'ja'}
        metadata.poster=lambda *_:'https://image.tmdb.org/t/p/w500/vertical.jpg'
        self.assertEqual(self.catalog.update_presentation_batch(limit=1),1)
        self.assertEqual(self.catalog.update_presentation_batch(limit=1),0)
        self.assertEqual(self.catalog.home_presentation(self.new)['homePoster'],'https://image.tmdb.org/t/p/w500/vertical.jpg')
        work=self.catalog.work('next-season')
        self.assertEqual(work['overviewZh'],'这是提供方的中文简介。')
        self.assertEqual(work['detail_artwork']['logos'],['https://image.tmdb.org/t/p/original/jp.png'])

    def test_legacy_empty_synopsis_and_empty_gallery_do_not_mask_verified_sources(self):
        root=self.catalog.config.root
        (root/'seed/tmdb-japanese-logos.json').write_text('{"tv:77":["/verified.png"]}',encoding='utf-8')
        with contextlib.closing(sqlite3.connect(self.catalog.seed)) as db:
            db.execute('CREATE TABLE provider_metadata(provider,external_id,payload)')
            payload={'tmdb_id':77,'media_type':'tv','backdrop_url':'https://image.tmdb.org/t/p/original/cached.jpg'}
            db.execute('INSERT INTO provider_metadata VALUES(?,?,?)',('TMDB','77',json.dumps(payload)))
            db.commit()
        self.store.cache_set('tmdb:artwork:v1:tv:77',{'backdrops':[],'logos':[]},60)
        self.store.cache_set('bangumi:overview-zh:200','',86400)
        work=self.catalog.present_detail(self.new)
        self.assertEqual(work['overviewZh'],'主角在小镇认识了新的朋友。')
        self.assertEqual(work['detail_artwork']['backdrops'],['https://image.tmdb.org/t/p/w1280/cached.jpg'])
        self.assertEqual(work['detail_artwork']['logos'],['https://image.tmdb.org/t/p/original/verified.png'])

    def test_backdrop_known_to_contain_text_is_not_restored(self):
        self.store.cache_set('tmdb:artwork:v1:tv:77',{'backdrops':[],'logos':[],
            'excluded_backdrops':['https://image.tmdb.org/t/p/w1280/marked.jpg']},60)
        work={**self.new,'backdrop_url':'https://image.tmdb.org/t/p/original/marked.jpg'}
        self.assertEqual(self.catalog.detail_artwork(work)['detail_artwork']['backdrops'],[])


class ChineseTranslations(unittest.TestCase):
    def test_traditional_chinese_is_used_when_simplified_chinese_is_missing(self):
        translations={'overview':'English description','translations':{'translations':[
            {'iso_639_1':'ja','iso_3166_1':'JP','data':{'overview':'日本語の紹介'}},
            {'iso_639_1':'zh','iso_3166_1':'TW','data':{'overview':'在未來的城鎮冒險。'}},
        ]}}
        self.assertEqual(Metadata.chinese_overview(translations),'在未来的城镇冒险。')
