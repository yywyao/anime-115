import contextlib
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
from mikan115.metadata import Metadata
from mikan115.store import Store


def image(path,language=None,width=1920,height=1080,score=5):
    return {'file_path':path,'iso_639_1':language,'width':width,'height':height,'vote_average':score}


class ArtworkSelection(unittest.TestCase):
    def test_only_neutral_landscape_backgrounds_and_japanese_logos_are_selected(self):
        gallery={'backdrops':[image('/ja-title.jpg','ja',score=10),image('/portrait.jpg',width=600,height=900),image('/neutral.jpg')],
                 'logos':[image('/zh.png','zh',score=10),image('/unknown.png'),image('/ja.png','ja',width=900,height=300)]}
        result=Metadata.select_artwork(gallery)
        self.assertEqual(result['backdrops'],['https://image.tmdb.org/t/p/w1280/neutral.jpg'])
        self.assertEqual(result['logos'],['https://image.tmdb.org/t/p/original/ja.png'])
        self.assertEqual(result['logo_language'],'ja')

    def test_empty_gallery_does_not_reintroduce_title_art_or_portrait_fallback(self):
        result=Metadata.select_artwork({'backdrops':[image('/with-text.jpg','en')],'logos':[]},legacy_backdrop='https://image.tmdb.org/t/p/original/old.jpg')
        self.assertEqual(result['backdrops'],[]);self.assertEqual(result['logos'],[])
        self.assertIsNone(result['logo_language'])

    def test_legacy_backdrop_and_logo_urls_cannot_use_other_hosts_or_bare_logo_language(self):
        for url in ['https://mikanani.me/images/poster.jpg','https://image.tmdb.org.evil.test/t/p/original/x.jpg','https://image.tmdb.org@evil.test/t/p/original/x.jpg','https://image.tmdb.org/t/p/original/x.jpg?token=private']:
            self.assertEqual(Metadata.select_artwork({},legacy_backdrop=url)['backdrops'],[])
        result=Metadata.select_artwork({'logo_url':'https://image.tmdb.org/t/p/original/unknown.png'},legacy_backdrop='https://image.tmdb.org/t/p/original/old.jpg')
        self.assertEqual(result['backdrops'],['https://image.tmdb.org/t/p/w1280/old.jpg'])
        self.assertEqual(result['logos'],[])

    def test_images_api_explicitly_requests_japanese_and_neutral_images(self):
        class HTTP:
            def __init__(self):self.requests=[]
            def json(self,method,url,headers=None):
                self.requests.append((method,urllib.parse.urlsplit(url),headers))
                return {'backdrops':[image('/neutral.jpg')],'logos':[image('/ja.png','ja')]}
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp));store.set_setting('tmdb_credentials',store.encrypt('synthetic-bearer-token'))
            http=HTTP();metadata=Metadata(store,http)
            for kind in ['tv','movie']:
                result=metadata.artwork(kind,77);self.assertTrue(result['logos'])
                _,url,headers=http.requests[-1]
                self.assertEqual(url.path,'/3/'+kind+'/77/images')
                self.assertEqual(urllib.parse.parse_qs(url.query),{'language':['ja'],'include_image_language':['ja,null']})
                self.assertNotIn('synthetic-bearer-token',url.geturl())
                self.assertEqual(headers['Authorization'],'Bearer synthetic-bearer-token')


class ArtworkCatalog(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'seed').mkdir()
        self.store=Store(self.root/'state');self.catalog=Catalog(self.store,Config(root=self.root,data=self.root/'state'))
        self.base={'id':'second','tmdb_id':77,'media_type':'series','defaultSeason':2,'title':'第二季','ratings':{'mal':8.2},
                   'poster_url':'https://mikanani.me/second.jpg','backdrop_url':'https://image.tmdb.org/t/p/original/current.jpg'}
        with contextlib.closing(sqlite3.connect(self.catalog.seed)) as db:
            db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE provider_metadata(provider TEXT,external_id TEXT,payload TEXT)')
            for ident,kind,path in [(77,'series','correct'),(77,'movie','wrong-kind'),(78,'series','wrong-id')]:
                payload={'tmdb_id':ident,'media_type':kind,'logos':[{'language':'ja','url':'https://image.tmdb.org/t/p/w500/'+path+'.png'}],
                         'poster_url':'https://image.tmdb.org/t/p/w500/first-season.jpg','ratings':{'mal':1}}
                db.execute('INSERT INTO works VALUES(?,?)',(path,json.dumps(payload)))
            db.commit()
    def tearDown(self):self.catalog.close();self.temp.cleanup()

    def test_seed_logo_uses_exact_tmdb_id_and_media_type_without_changing_other_data(self):
        result=self.catalog.detail_artwork(self.base)
        self.assertEqual(result['detail_artwork']['logos'],['https://image.tmdb.org/t/p/original/correct.png'])
        for key,value in self.base.items():self.assertEqual(result[key],value)
        self.assertFalse(self.catalog.metadata.status()['tmdb_configured'])

    def test_conflicted_or_multiple_work_identity_has_no_artwork(self):
        for extra in [{'tmdb_link_conflict':True},{'tmdb_mapping':{'status':'multiple_works'}}]:
            result=self.catalog.detail_artwork({**self.base,**extra})
            self.assertEqual(result['detail_artwork']['logos'],[]);self.assertEqual(result['detail_artwork']['backdrops'],[])

    def test_refresh_is_nonblocking_coalesced_and_retains_stale_art_on_failure(self):
        key='tmdb:artwork:v1:tv:77';old={'backdrops':['https://image.tmdb.org/t/p/w1280/old.jpg'],'logos':['https://image.tmdb.org/t/p/original/old.png'],'logo_language':'ja'}
        self.store.cache_set(key,old,-1);self.store.set_setting('tmdb_credentials',self.store.encrypt('synthetic'))
        entered=threading.Event();release=threading.Event();calls=[]
        def fail(*args):
            calls.append(args);entered.set();release.wait(2);raise RuntimeError('artificial upstream failure')
        self.catalog.metadata.artwork=fail
        try:
            start=time.monotonic()
            first=self.catalog.detail_artwork(self.base)
            self.assertLess(time.monotonic()-start,.5);self.assertTrue(entered.wait(1))
            second=self.catalog.detail_artwork(self.base)
            self.assertEqual(first['detail_artwork']['logos'],old['logos']);self.assertEqual(second['detail_artwork']['backdrops'],old['backdrops'])
            self.assertEqual(len(calls),1)
        finally:release.set()

    def test_refreshed_gallery_overrides_legacy_art_without_replacing_season_or_ratings(self):
        gallery={'backdrops':['https://image.tmdb.org/t/p/w1280/new.jpg'],'logos':['https://image.tmdb.org/t/p/original/new.png'],'logo_language':'ja'}
        self.store.cache_set('tmdb:artwork:v1:tv:77',gallery,60)
        result=self.catalog.detail_artwork(self.base)
        self.assertEqual(result['detail_artwork']['backdrops'],gallery['backdrops'])
        self.assertEqual(result['defaultSeason'],2);self.assertEqual(result['ratings'],self.base['ratings'])


if __name__=='__main__':unittest.main()
