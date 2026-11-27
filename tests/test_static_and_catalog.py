import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.errors import ProviderError
from mikan115.store import Store


class FrontendAndCatalog(unittest.TestCase):
    def test_relative_mikan_original_cover_uses_the_provider_origin(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);catalog=Catalog(Store(root/'state'),Config(root=root,data=root/'state'))
            result=catalog.decorate({'mikan_id':'42','image':'/images/Bangumi/202501/source.jpg?width=400&height=400'})
            self.assertEqual(result['poster_url'],'https://mikanani.me/images/Bangumi/202501/source.jpg?width=400')
            self.assertEqual(result['mikan_poster_url'],result['poster_url'])

    def test_mikan_square_thumbnail_keeps_original_poster_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);catalog=Catalog(Store(root/'state'),Config(root=root,data=root/'state'))
            item={'bangumiId':'9988','title':'测试作品','image':'https://mikanani.me/images/Bangumi/202610/poster.jpg?width=400&height=400&format=webp'}
            result=catalog.decorate(item)
            self.assertEqual(result['image'],'https://mikanani.me/images/Bangumi/202610/poster.jpg?width=400&format=webp')
            self.assertEqual(result['poster_url'],result['image'])
            self.assertIn('height=400',item['image'])
            foreign='https://image.tmdb.org/t/p/w500/poster.jpg?height=400'
            self.assertEqual(catalog.decorate({'image':foreign})['image'],foreign)

    def test_active_bundle_has_new_routes_and_no_legacy_account_or_approval_ui(self):
        root=Path(__file__).resolve().parents[1]
        s=(root/'static/assets/mikan115-app.js').read_text(encoding='utf-8')
        self.assertNotIn('emby',s.lower());self.assertNotIn('审批',s);self.assertNotIn('求片',s)
        self.assertIn('window.Mikan115',s);self.assertIn('/auth/login',s);self.assertIn('/static/mikan115-ui.js',s)
        report=json.loads((root/'frontend/ADAPTER-REPORT.json').read_text())
        self.assertEqual(report['legacy_request_modal_removed'],1);self.assertEqual(report['detail_transfer_action'],1)

    def test_seed_fallback_does_not_guess_unknown_weekday(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'seed').mkdir();store=Store(root/'state');config=Config(root=root,data=root/'state')
            catalog=Catalog(store,config);current=catalog.current()
            c=sqlite3.connect(root/'seed/catalog.sqlite');c.execute('CREATE TABLE works(id,mikan_id,title,year,season,weekday,payload)')
            for ident,day in [('known',4),('unknown',None)]:
                payload={'id':ident,'title':ident,'air_weekday':day}
                c.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',(ident,'',ident,current['year'],current['season'],day,json.dumps(payload)))
            c.commit();c.close()
            def offline(*args,**kwargs):raise ProviderError()
            catalog.fetch=offline
            result=catalog.schedule();self.assertEqual(result['source'],'snapshot_fallback');self.assertTrue(result['source_notice'])
            self.assertEqual(sum(len(x['items']) for x in result['days']),1)
            self.assertEqual(len(result['bangumis']),2);self.assertEqual(result['unknownItems'][0]['id'],'unknown')


if __name__=='__main__':unittest.main()
