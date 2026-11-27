"""Complete a public-only portable catalog; never read application credentials."""
import argparse,collections,concurrent.futures,contextlib,hashlib,json,os,pathlib,sqlite3,sys,tempfile,time
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mikan115.catalog import Catalog
from mikan115.catalog_sync import CrossReferences,PublicFeeds
from mikan115.config import Config
from mikan115.store import Store
from tools.restore_public_metadata import safe

def complete(catalog,source_folder,first,last):
    folder=pathlib.Path(source_folder);manifest=json.loads((folder/'mapping-manifest.json').read_bytes())
    raw=(folder/'mapping-subjects.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==manifest['files']['subjects.json']['sha256']
    index=CrossReferences(json.loads((folder/'bgdata.json').read_bytes()),json.loads((folder/'fribb.json').read_bytes()),json.loads(raw))
    before=catalog.all_works()
    original_images={str(r['source_id']):r['source_image'] for r in catalog.seed_query("SELECT source_id,source_image FROM identities WHERE source_type='MIKAN_BANGUMI'") if r.get('source_image')}
    values=[index.enrich(catalog.decorate({**w,**({'mikan_poster_url':original_images[str(w['mikan_id'])]} if str(w.get('mikan_id')) in original_images else {})},lookup=False)) for w in before]
    values+=index.missing_works(values,first,last+1);catalog.save_works(values)
    seasons=[]
    def quarter(item):
        year,season=item
        try:
            data=catalog.load_schedule(year,season);catalog.store.cache_set(f'schedule:v2:{year}:{season}',data,86400)
            catalog.sync_result(f'quarter:{year}:{season}',{'works':len(data['bangumis'])},86400)
            return {'year':year,'season':season,'works':len(data['bangumis']),'success':True}
        except Exception:return {'year':year,'season':season,'success':False}
    quarters=[(y,s) for y in range(first,last+1) for s in ['冬','春','夏','秋']]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(quarter,quarters):seasons.append(result);print('QUARTER',json.dumps(result,ensure_ascii=False),flush=True)
    works=catalog.all_works();pending=[w for w in works if w.get('mikan_id') and (not (w.get('provider_ids') or {}).get('bangumi') or not w.get('mikan_poster_url'))]
    def details(work):
        try:
            value=catalog.mikan_detail(work['mikan_id']);return index.enrich({**work,**value,'id':work['id'],'canonical_id':work.get('canonical_id') or work['id']})
        except Exception:return work
    print('RESOLVING_PUBLIC_MIKAN_DETAILS',len(pending),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        batch=[]
        for i,value in enumerate(pool.map(details,pending),1):
            batch.append(value)
            if len(batch)>=50:catalog.save_works(batch);batch=[];print('DETAILS',i,'/',len(pending),flush=True)
        if batch:catalog.save_works(batch)
    final=[index.enrich(w) for w in catalog.all_works()];catalog.save_works(final)
    stats={'first_year':first,'last_year':last,'quarters':seasons,'quarters_checked':len(seasons),'quarters_available':sum(q['success'] for q in seasons),'works_before':len(before),'works_after':len(final),'tmdb_before':sum(bool(w.get('tmdb_id')) for w in before),'tmdb_after':sum(bool(w.get('tmdb_id')) for w in final),'unresolved_tmdb':sum(not bool(w.get('tmdb_id')) for w in final),'conflicts_preserved':sum(bool(w.get('tmdb_link_conflict')) for w in final),'mikan_original_posters':sum(bool(w.get('mikan_poster_url')) for w in final),'source_commit':manifest['sourceCommit'],'real_115_login':False,'external_ai_service_used':False,'matching':'Explicit provider ID cross references; ambiguous mappings are not guessed.'}
    catalog.sync_result('references',stats,86400)
    return stats

def export(catalog,stats):
    seed=ROOT/'seed/catalog.sqlite';fd,name=tempfile.mkstemp(prefix='completed-',suffix='.sqlite',dir=seed.parent);os.close(fd);target=pathlib.Path(name)
    try:
        with contextlib.closing(sqlite3.connect('file:'+seed.as_posix()+'?mode=ro&immutable=1',uri=True)) as original,contextlib.closing(sqlite3.connect(target)) as dst:
            original.backup(dst)
            for w in catalog.all_works():
                work=safe(w);dst.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mikan_id=excluded.mikan_id,title=excluded.title,year=excluded.year,season=excluded.season,weekday=excluded.weekday,payload=excluded.payload',(work['id'],work.get('mikan_id',''),work.get('title',''),work.get('year'),work.get('release_season'),work.get('air_weekday'),json.dumps(work,ensure_ascii=False)))
            dst.execute('CREATE TABLE IF NOT EXISTS catalog_seasons(year INTEGER,season TEXT,payload TEXT,updated_at REAL,PRIMARY KEY(year,season))')
            for q in stats['quarters']:
                data=catalog.store.cache_get(f"schedule:v2:{q['year']}:{q['season']}",stale=True)
                if data:
                    # Re-decorate stored schedules after new source identities have been resolved.
                    for group in data['days']:
                        group['items']=[catalog.summary(catalog.decorate(x)) for x in group['items']]
                    data['bangumis']=[x for group in data['days'] for x in group['items']]
                    dst.execute('INSERT OR REPLACE INTO catalog_seasons VALUES(?,?,?,?)',(q['year'],q['season'],json.dumps(safe(data),ensure_ascii=False),time.time()))
            dst.commit();assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        target.chmod(0o600);os.replace(target,seed)
        info=json.loads((ROOT/'SOURCE.json').read_text());info['public_metadata']['works']=stats['works_after'];info['public_completion']=stats
        (ROOT/'SOURCE.json').write_text(json.dumps(info,ensure_ascii=False,indent=2))
    finally:
        if target.exists():target.unlink()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--sources',type=pathlib.Path,required=True);p.add_argument('--state',type=pathlib.Path,required=True);p.add_argument('--first',type=int,default=2012);p.add_argument('--last',type=int,default=time.gmtime().tm_year);p.add_argument('--export',action='store_true');a=p.parse_args()
    cfg=Config(root=ROOT,data=a.state);catalog=Catalog(Store(a.state),cfg)
    try:
        stats=complete(catalog,a.sources,a.first,a.last)
        if a.export:export(catalog,stats)
        dest=ROOT/'verification/catalog-completion.json';dest.parent.mkdir(exist_ok=True);dest.write_text(json.dumps(stats,ensure_ascii=False,indent=2))
        print('PUBLIC_CATALOG_COMPLETED',json.dumps({k:v for k,v in stats.items() if k!='quarters'},ensure_ascii=False),flush=True)
    finally:catalog.close()
