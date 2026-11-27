import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

class PublicRestore(unittest.TestCase):
    def test_restoration_keeps_seasons_scores_and_private_data_separate(self):
        script=Path(__file__).resolve().parents[1]/'tools/restore_public_metadata.py'
        spec=importlib.util.spec_from_file_location('public_restore_test',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=root/'app';reference=root/'reference';(reference/'legacy-data').mkdir(parents=True);(app/'seed').mkdir(parents=True)
            (root/'SOURCE-AND-ISOLATION.json').write_text('{}');(app/'SOURCE.json').write_text('{}')
            source=reference/'legacy-data/mikan.db'
            with contextlib.closing(sqlite3.connect(source,isolation_level=None)) as db:
                db.executescript('''
                CREATE TABLE catalog_title_identity(id,source_type,source_item_id,title,original_title,year,season,canonical_series_id,mapping_status,mapping_confidence,source_image);
                CREATE TABLE title_external_mappings(identity_id,provider,external_id,confidence,manually_confirmed,active);
                CREATE TABLE provider_ratings(id,canonical_work_id,identity_id,provider,provider_id,score,score_scale,vote_count,fetched_at);
                CREATE TABLE provider_metadata_cache(provider,external_id,payload_json);
                CREATE TABLE tmdb_metadata_cache(tmdb_id,payload_json);
                CREATE TABLE tmdb_season_artwork_cache(cache_key,payload_json);
                CREATE TABLE autonomous_catalog(anilist_id,identity_id,mikan_id,tmdb_id,tmdb_season,air_date);
                CREATE TABLE canonical_series(id,tmdb_id,tvdb_id);
                CREATE TABLE users(username,password);
                INSERT INTO users VALUES('private-user','PRIVATE_PASSWORD_MUST_NOT_COPY');
                ''')
                for identity,bid,aid,title,season in [(41,'1001','10001','测试作品',1),(42,'4098','10002','测试作品 第二季',2)]:
                    db.execute('INSERT INTO catalog_title_identity VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,'MIKAN_BANGUMI',bid,title,None,2026,str(season),None,'CONFIRMED',.99,'https://mikanani.me/images/Bangumi/202610/'+bid+'.jpg'))
                    db.execute('INSERT INTO title_external_mappings VALUES(?,?,?,?,?,?)',(identity,'ANILIST',aid,.99,0,1))
                    db.execute('INSERT INTO autonomous_catalog VALUES(?,?,?,?,?,?)',(int(aid),identity,None,None,None,'2026-10-01'))
                for provider,external,score in [('MAL','64534',8.42),('BANGUMI','665390',7.4),('TMDB','283428',8.6)]:
                    db.execute('INSERT INTO title_external_mappings VALUES(?,?,?,?,?,?)',(42,provider,external,.99,0,1))
                    db.execute('INSERT INTO provider_ratings VALUES(?,?,?,?,?,?,?,?,?)',(len(provider),None,42,provider,external,score,10,100,'2026-10-08'))
                db.execute('INSERT INTO provider_metadata_cache VALUES(?,?,?)',('TMDB','283428',json.dumps({'backdrop_url':'https://image.tmdb.org/t/p/original/background.jpg','api_key':'PRIVATE_API_MUST_NOT_COPY'})))
                db.execute('INSERT INTO tmdb_season_artwork_cache VALUES(?,?)',('283428:2',json.dumps({'status':'RESOLVED','season_number':2,'season_poster_url':'https://image.tmdb.org/t/p/w500/season-2.jpg'})))
            seed=app/'seed/catalog.sqlite'
            with contextlib.closing(sqlite3.connect(seed,isolation_level=None)) as db:
                db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,mikan_id,title,year,season,weekday,payload)')
                for aid,title in [('10001','测试作品'),('10002','测试作品 第二季')]:
                    payload={'id':'catalog~'+aid,'title':title,'year':2026,'provider_ids':{'anilist':aid},'image':'','poster_url':''}
                    db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',(payload['id'],'',title,2026,'秋',4,json.dumps(payload)))
            digest=hashlib.sha256(source.read_bytes()).hexdigest();module.ROOT=app
            with patch.object(sys,'argv',[str(script),'--reference',str(reference)]),contextlib.redirect_stdout(io.StringIO()):module.main()
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
            with contextlib.closing(sqlite3.connect(seed)) as db:
                self.assertNotIn('users',[r[0] for r in db.execute("select name from sqlite_master where type='table'")])
                first=json.loads(db.execute("SELECT payload FROM works WHERE mikan_id='1001'").fetchone()[0])
                second=json.loads(db.execute("SELECT payload FROM works WHERE mikan_id='4098'").fetchone()[0])
                self.assertEqual(first['title'],'测试作品');self.assertEqual(second['defaultSeason'],2)
                self.assertEqual(second['ratings'],{'mal':8.42,'bangumi':7.4,'tmdb':8.6})
                self.assertTrue(second['poster_url'].endswith('season-2.jpg'))
                self.assertNotIn('mal',first['ratings'])
            self.assertNotIn(b'PRIVATE_PASSWORD_MUST_NOT_COPY',seed.read_bytes())
            self.assertNotIn(b'PRIVATE_API_MUST_NOT_COPY',seed.read_bytes())

if __name__=='__main__':unittest.main()
