"""Restore public identities, provider scores and artwork into the portable seed."""
import argparse
import collections
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def safe(value):
    if isinstance(value,dict):
        return {k:safe(v) for k,v in value.items() if not any(x in k.lower() for x in
            ('password','secret','token','cookie','api_key','session','emby','sonarr','user_id','asset_id','remote_path'))}
    if isinstance(value,list):return [safe(v) for v in value]
    if isinstance(value,str) and value.startswith(('/opt/','/data/','/mnt/','/quest/api/')):return ''
    return value

def load(value,default=None):
    try:return json.loads(value) if value else ({} if default is None else default)
    except (ValueError,TypeError):return {} if default is None else default

def display_season(title):
    match=re.search(r'第\s*(\d+|[一二三四五六七八九十]+)\s*[季期]',str(title))
    if match:
        value=match[1]
        if value.isdigit():return int(value)
        units={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
        return units.get(value,1)
    match=re.search(r'(\d+)(?:st|nd|rd|th)\s+Season|Season\s+(\d+)',str(title),re.I)
    return int(next(x for x in match.groups() if x)) if match else 1

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reference',type=Path,required=True);args=parser.parse_args()
    reference=args.reference.resolve()
    if reference.is_relative_to(ROOT) or not (reference.parent/'SOURCE-AND-ISOLATION.json').is_file():raise SystemExit('An isolated reference outside the application is required.')
    source=reference/'legacy-data/mikan.db';before=(source.stat().st_size,source.stat().st_mtime_ns)
    src=sqlite3.connect('file:'+str(source)+'?mode=ro&immutable=1',uri=True);src.row_factory=sqlite3.Row
    seed=ROOT/'seed/catalog.sqlite'
    fd,tempname=tempfile.mkstemp(prefix='public-catalog-',suffix='.sqlite',dir=seed.parent);os.close(fd)
    target=Path(tempname)
    dst=sqlite3.connect(target);dst.row_factory=sqlite3.Row
    try:
        with contextlib.closing(sqlite3.connect('file:'+str(seed).replace('\\','/')+'?mode=ro&immutable=1',uri=True)) as original:original.backup(dst)
        dst.executescript('''
        CREATE TABLE IF NOT EXISTS identities(id INTEGER PRIMARY KEY,source_type TEXT,source_id TEXT,title TEXT,original_title TEXT,year INTEGER,season TEXT,canonical_id TEXT,status TEXT,confidence REAL,source_image TEXT);
        CREATE INDEX IF NOT EXISTS identity_source ON identities(source_type,source_id);
        CREATE TABLE IF NOT EXISTS external_mappings(identity_id INTEGER,provider TEXT,external_id TEXT,confidence REAL,PRIMARY KEY(identity_id,provider,external_id));
        CREATE INDEX IF NOT EXISTS mapping_provider ON external_mappings(provider,external_id);
        CREATE TABLE IF NOT EXISTS provider_ratings(id INTEGER PRIMARY KEY,canonical_id TEXT,identity_id INTEGER,provider TEXT,provider_id TEXT,score REAL,scale REAL,votes INTEGER,fetched_at TEXT);
        CREATE INDEX IF NOT EXISTS rating_identity ON provider_ratings(identity_id);
        CREATE INDEX IF NOT EXISTS rating_provider ON provider_ratings(provider,provider_id);
        CREATE TABLE IF NOT EXISTS provider_metadata(provider TEXT,external_id TEXT,payload TEXT,PRIMARY KEY(provider,external_id));
        CREATE TABLE IF NOT EXISTS season_artwork(cache_key TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS seed_info(key TEXT PRIMARY KEY,value TEXT);
        ''')
        for table in ['identities','external_mappings','provider_ratings','provider_metadata','season_artwork']:dst.execute('DELETE FROM '+table)
        identities={}
        for row in src.execute('SELECT * FROM catalog_title_identity'):
            r=dict(row);identities[r['id']]=r
            dst.execute('INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?,?)',(r['id'],r['source_type'],str(r['source_item_id']),r['title'],r['original_title'],r['year'],r['season'],('series:'+str(r['canonical_series_id'])) if r['canonical_series_id'] else '',r['mapping_status'],r['mapping_confidence'],safe(r['source_image'])))
        mappings=collections.defaultdict(dict);provider_identities=collections.defaultdict(set)
        for r in src.execute('SELECT identity_id,provider,external_id,confidence,manually_confirmed FROM title_external_mappings WHERE active=1'):
            if not r['manually_confirmed'] and float(r['confidence'] or 0)<.9:continue
            provider=str(r['provider']).upper();external=str(r['external_id'])
            if provider not in {'TMDB','TVDB','MAL','BANGUMI','ANILIST'} or not external.isdigit():continue
            dst.execute('INSERT OR REPLACE INTO external_mappings VALUES(?,?,?,?)',(r['identity_id'],provider,external,r['confidence']))
            mappings[r['identity_id']][provider.lower()]=external
            provider_identities[(provider,external)].add(r['identity_id'])
        ratings_by_identity=collections.defaultdict(dict);ratings_by_canonical=collections.defaultdict(dict);ratings_by_provider={}
        for r in src.execute('SELECT * FROM provider_ratings ORDER BY fetched_at,id'):
            provider=str(r['provider']).upper();score=r['score'];scale=r['score_scale'] or 10
            if score is None or not 0<float(score)<=float(scale):continue
            normalized=float(score)*10/float(scale)
            dst.execute('INSERT INTO provider_ratings VALUES(?,?,?,?,?,?,?,?,?)',(r['id'],r['canonical_work_id'],r['identity_id'],provider,str(r['provider_id'] or ''),normalized,10,r['vote_count'],r['fetched_at']))
            data={'score':normalized,'votes':r['vote_count'],'fetched_at':r['fetched_at'],'provider_id':str(r['provider_id'] or '')}
            if r['identity_id']:ratings_by_identity[r['identity_id']][provider.lower()]=data
            if r['canonical_work_id']:ratings_by_canonical[r['canonical_work_id']][provider.lower()]=data
            if r['provider_id']:ratings_by_provider[(provider,str(r['provider_id']))]=data
        metadata={}
        for r in src.execute('SELECT provider,external_id,payload_json FROM provider_metadata_cache'):
            provider=str(r['provider']).upper();external=str(r['external_id']);payload=safe(load(r['payload_json']))
            metadata[(provider,external)]=payload
            dst.execute('INSERT OR REPLACE INTO provider_metadata VALUES(?,?,?)',(provider,external,json.dumps(payload,ensure_ascii=False)))
        for r in src.execute('SELECT tmdb_id,payload_json FROM tmdb_metadata_cache'):
            payload=safe(load(r['payload_json']));external=str(r['tmdb_id'])
            metadata[('TMDB_DETAIL',external)]=payload
            dst.execute('INSERT OR REPLACE INTO provider_metadata VALUES(?,?,?)',('TMDB_DETAIL',external,json.dumps(payload,ensure_ascii=False)))
        artworks={}
        for r in src.execute('SELECT cache_key,payload_json FROM tmdb_season_artwork_cache'):
            payload=safe(load(r['payload_json']));artworks[r['cache_key']]=payload
            dst.execute('INSERT INTO season_artwork VALUES(?,?)',(r['cache_key'],json.dumps(payload,ensure_ascii=False)))
        autonomous={str(r['anilist_id']):dict(r) for r in src.execute('SELECT anilist_id,identity_id,mikan_id,tmdb_id,tmdb_season,air_date FROM autonomous_catalog')}
        canonical={str(r['id']):dict(r) for r in src.execute('SELECT id,tmdb_id,tvdb_id FROM canonical_series')}
        mapped=0;rated=0
        for row in dst.execute('SELECT * FROM works').fetchall():
            work=load(row['payload']);providers={str(k).lower():str(v) for k,v in (work.get('provider_ids') or {}).items() if v}
            a=autonomous.get(str(providers.get('anilist') or ''),{})
            if row['id'].startswith('series:'):
                c=canonical.get(row['id'].split(':',1)[1],{})
                for key in ['tmdb','tvdb']:
                    if c.get(key+'_id'):providers[key]=str(c[key+'_id'])
            candidates=set()
            for key in ['anilist','mal','bangumi']:
                if providers.get(key):
                    candidates.update(i for i in provider_identities.get((key.upper(),providers[key]),set()) if identities.get(i,{}).get('source_type')=='MIKAN_BANGUMI')
            if a.get('identity_id') in identities:candidates.add(a['identity_id'])
            # An exact external subject ID may have several Mikan entries. Do
            # not resolve that ambiguity using title similarity.
            chosen=next(iter(candidates)) if len(candidates)==1 else None
            if chosen:
                identity=identities[chosen];work['identity_id']=chosen
                providers={**providers,**mappings[chosen]}
                bid=str(identity['source_item_id'])
                if identity['source_type']=='MIKAN_BANGUMI' and bid.isdigit():
                    work.update(mikan_id=bid,mikanBangumiId=bid,bangumiId=bid);mapped+=1
                    if not work.get('poster_url') and identity.get('source_image'):work['poster_url']=work['image']=identity['source_image']
            work['provider_ids']=providers
            if providers.get('tmdb'):work['tmdb_id']=int(providers['tmdb'])
            public=metadata.get(('TMDB',str(providers.get('tmdb') or '')),{})
            if public.get('backdrop_url') and not work.get('backdrop_url'):work['backdrop_url']=public['backdrop_url']
            selected=display_season(work.get('title') or work.get('title_original'))
            if selected==1 and not work.get('poster_url') and public.get('poster_url'):
                work['poster_url']=work['image']=public['poster_url']
            if selected>1:
                work['defaultSeason']=selected;work['display_season']=selected
                # A confirmed Mikan poster belongs to this exact subject even
                # when a provider groups it under the parent television series.
                identity=identities.get(chosen,{})
                if identity.get('source_image'):work['poster_url']=work['image']=identity['source_image']
                art=artworks.get(str(providers.get('tmdb',''))+':'+str(selected),{})
                if art.get('status')=='RESOLVED' and int(art.get('season_number') or 0)==selected:
                    work['poster_url']=work['image']=art.get('season_poster_url') or work.get('poster_url','')
            scores={**ratings_by_canonical.get(row['id'],{}),**ratings_by_identity.get(chosen,{})}
            for provider,external in providers.items():
                if provider not in scores and (provider.upper(),external) in ratings_by_provider:scores[provider]=ratings_by_provider[(provider.upper(),external)]
            valid={k:v for k,v in (work.get('ratings') or {}).items() if isinstance(v,(int,float)) and v>0}
            valid.update({k:v['score'] for k,v in scores.items() if k in {'tmdb','bangumi','mal'}})
            work['ratings']=valid;work['rating_details']=scores
            if valid:rated+=1
            if a.get('air_date'):work.setdefault('air_date',a['air_date'])
            work=safe(work)
            dst.execute('UPDATE works SET mikan_id=?,payload=? WHERE id=?',(str(work.get('mikan_id') or ''),json.dumps(work,ensure_ascii=False),row['id']))
        counts={table:dst.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ['works','identities','external_mappings','provider_ratings','provider_metadata','season_artwork']}
        dst.execute('INSERT OR REPLACE INTO seed_info VALUES(?,?)',('public_metadata_version','2'))
        dst.commit();assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        dst.close();src.close()
        assert before==(source.stat().st_size,source.stat().st_mtime_ns)
        target.chmod(0o600);os.replace(target,seed)
        info_path=ROOT/'SOURCE.json';info=load(info_path.read_text());info['public_metadata']=counts
        info['copied_legacy_credentials']=False;info['copied_private_accounts']=False
        info_path.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'restored_public_metadata':counts,'mapped_works':mapped,'rated_works':rated,'reference_unchanged':True}),flush=True)
    finally:
        try:dst.close()
        except Exception:pass
        src.close()
        if target.exists():target.unlink()

if __name__=='__main__':main()
