"""One-time import from the isolated reference, never from a live installation."""
import argparse,ast,hashlib,json,pathlib,re,shutil,sqlite3

ROOT=pathlib.Path(__file__).resolve().parents[1]
PARSER_NAMES={'parse_episode_info','parse_mikan_table_rows','parse_season_and_clean_title',
 '_mikan_tag_attributes','_mikan_plain_text','_mikan_weekday_number','_parse_mikan_weekday_schedule'}

def make_parsers(main):
    source=main.read_text(encoding='utf-8');tree=ast.parse(source)
    pieces=[]
    for n in tree.body:
        if isinstance(n,ast.FunctionDef) and n.name in PARSER_NAMES:
            text=ast.get_source_segment(source,n).replace('        from mapping_policy import scope_number\n','')
            if n.name=='parse_mikan_table_rows':
                text=text.replace("rows = re.findall(r'<tr[^>]*>([\\s\\S]*?)</tr>', table_html)","rows = re.findall(r'<tr\\b[^>]*>[\\s\\S]*?</tr>', table_html)")
            pieces.append(text)
    if len(pieces)!=len(PARSER_NAMES):raise RuntimeError('Reference parser functions changed')
    header='''"""Pure parsers retained from the supplied Mikan site; no legacy service imports."""
import html
import re
CN_NUMS={"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
def scope_number(value):
    if str(value).isdigit():return int(value)
    if "十" in value:
        a,b=value.split("十",1);return CN_NUMS.get(a,1)*10+CN_NUMS.get(b,0)
    return CN_NUMS.get(value)
def parse_size_to_bytes(value):
    m=re.fullmatch(r"\\s*(\\d+(?:\\.\\d+)?)\\s*([KMGT]?I?B)\\s*",str(value),re.I)
    if not m:return None
    unit=m.group(2).upper().replace("I","");return int(float(m.group(1))*1024**{"B":0,"KB":1,"MB":2,"GB":3,"TB":4}[unit])
'''
    target=ROOT/'mikan115/mikan_parser.py';target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(header+'\n\n'+'\n\n'.join(pieces)+'\n',encoding='utf-8')

def safe_json(s,default):
    try:return json.loads(s) if s else default
    except (ValueError,TypeError):return default

def clean(value):
    if isinstance(value,dict):
        return {k:clean(v) for k,v in value.items() if not any(x in k.lower() for x in ('emby','sonarr','password','secret','token','session','user_id','asset_id','owned','remote_path'))}
    if isinstance(value,list):return [clean(v) for v in value]
    if isinstance(value,str) and value.startswith(('/quest/api/artwork/','/opt/','/mnt/','/data/')):return ''
    return value

def import_catalog(source):
    src=sqlite3.connect('file:'+str(source.resolve()).replace('\\','/')+'?mode=ro&immutable=1',uri=True);src.row_factory=sqlite3.Row
    seed=ROOT/'seed/catalog.sqlite';seed.parent.mkdir(exist_ok=True)
    if seed.exists():raise RuntimeError('Refusing to overwrite an existing catalog seed')
    dst=sqlite3.connect(seed)
    dst.execute('CREATE TABLE works(id TEXT PRIMARY KEY,mikan_id TEXT,title TEXT,year INTEGER,season TEXT,weekday INTEGER,payload TEXT NOT NULL)')
    rows=0
    def put(w):
        nonlocal rows
        w=clean(w);w['inLibrary']=False;w['in_library']=False;w['playable']=False
        w['source_snapshot']='2026-10-08T16:15:00Z'
        dst.execute('INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?)',(w['id'],str(w.get('mikan_id') or ''),w.get('title',''),w.get('year'),w.get('release_season'),w.get('air_weekday'),json.dumps(w,ensure_ascii=False)))
        rows+=1
    for r in src.execute('SELECT * FROM autonomous_catalog'):
        detail=safe_json(r['detail_json'],{});anilist=safe_json(r['anilist_json'],{})
        aid=r['anilist_id'];ident='catalog~'+str(aid);mikan=str(r['mikan_id'] or '')
        poster=r['poster_url'] or (anilist.get('coverImage') or {}).get('extraLarge') or (anilist.get('coverImage') or {}).get('large') or ''
        title=r['title_zh'] or r['title_original']
        payload={**detail,'id':ident,'canonical_id':ident,'bangumiId':ident,'mikan_id':mikan,'mikanBangumiId':mikan,
                 'title':title,'title_zh':r['title_zh'],'title_original':r['title_original'],'year':r['release_year'],
                 'release_season':r['release_season'],'air_weekday':r['air_weekday'],
                 'status':r['status'],'overview':detail.get('overview') or r['description'],
                 'image':poster,'poster_url':poster,'backdrop_url':detail.get('backdrop_url') or anilist.get('bannerImage') or '',
                 'episodes':detail.get('episodes') or [],'seasons':detail.get('seasons') or [],
                 'media_type':'movie' if r['format']=='MOVIE' else 'series','format':r['format'],
                 'defaultSeason':r['tmdb_season'] or 1,'downloadSeason':r['tvdb_season'] or 1,
                 'provider_ids':{k:str(v) for k,v in {'anilist':aid,'mal':r['mal_id'],'bangumi':r['bangumi_id'],'tmdb':r['tmdb_id'],'tvdb':r['tvdb_id']}.items() if v},
                 'tmdb_id':r['tmdb_id'],'subgroups':[]}
        put(payload)
    candidates={}
    for row in src.execute("SELECT canonical_series_id,source_item_id FROM catalog_title_identity WHERE source_type='MIKAN_BANGUMI' AND canonical_series_id IS NOT NULL AND mapping_status='CONFIRMED'"):
        candidates.setdefault(str(row['canonical_series_id']),set()).add(str(row['source_item_id']))
    mikan_map={k:next(iter(v)) for k,v in candidates.items() if len(v)==1}
    for r in src.execute('SELECT * FROM canonical_series'):
        art=safe_json(r['artwork_json'],{});providers=safe_json(r['provider_ids_json'],{})
        episodes=[]
        for e in src.execute('SELECT * FROM canonical_episodes WHERE canonical_series_id=? ORDER BY season_number,episode_number',(r['id'],)):
            episodes.append({k:e[k] for k in ['id','season_number','episode_number','title','title_zh','title_original','overview','overview_zh','air_date','still_url','provider_episode_id']})
        mikan=mikan_map.get(str(r['id']),'');ident='series:'+str(r['id'])
        put({**art,'id':ident,'canonical_id':ident,'canonicalSeriesId':r['id'],'bangumiId':mikan or ident,
             'mikan_id':mikan,'mikanBangumiId':mikan,'title':r['title_zh'] or r['title_original'],'title_zh':r['title_zh'],
             'title_original':r['title_original'],'year':r['year'],'status':r['status'],'overview':r['overview_zh'] or r['overview'],
             'poster_url':r['poster_url'] or '', 'image':r['poster_url'] or '', 'backdrop_url':r['backdrop_url'] or '',
             'logo_url':r['logo_url'] or '', 'provider_ids':providers,'episodes':episodes,'media_type':'series',
             'defaultSeason':1,'subgroups':[]})
    dst.execute('CREATE INDEX works_mikan ON works(mikan_id)');dst.execute('CREATE INDEX works_season ON works(year,season)')
    dst.commit();check=dst.execute('PRAGMA quick_check').fetchone()[0];count=dst.execute('SELECT count(*) FROM works').fetchone()[0]
    dst.close();src.close()
    print('PUBLIC_CATALOG_IMPORTED',count,'quick_check='+check,flush=True)
    return count

def main():
    p=argparse.ArgumentParser();p.add_argument('--reference',type=pathlib.Path);p.add_argument('--main-file',type=pathlib.Path);p.add_argument('--parsers-only',action='store_true');a=p.parse_args()
    main_file=a.main_file or a.reference/'opt/media-server/mikan-manager/mikan_manager.py'
    make_parsers(main_file)
    if a.parsers_only:return
    reference=a.reference.resolve();original=reference/'opt/media-server/mikan-manager'
    if reference==ROOT or reference.is_relative_to(ROOT):raise RuntimeError('Reference must be outside the active application')
    if not (reference.parent/'SOURCE-AND-ISOLATION.json').exists():raise RuntimeError('Isolation marker is missing')
    (ROOT/'frontend').mkdir(exist_ok=True);(ROOT/'static/assets').mkdir(parents=True,exist_ok=True);(ROOT/'templates').mkdir(exist_ok=True)
    baseline=original/'static/assets/index-request-season-groups-20261007.js'
    digest=hashlib.sha256(baseline.read_bytes()).hexdigest()
    if digest!='9bff4ebc86ff7ef42c19a3e3b6c70e98d58801b840e654b6abe152ae20090778':raise RuntimeError('Frontend baseline changed')
    for name in ['index-B7Y-TVlk.css','admin-viewport-fixes.css','detail-interaction-fixes.css']:
        shutil.copyfile(original/'static/assets'/name,ROOT/'static/assets'/name)
    if (original/'static/provider-logos').exists():shutil.copytree(original/'static/provider-logos',ROOT/'static/provider-logos',dirs_exist_ok=True)
    html=(original/'templates/index.html').read_text(encoding='utf-8')
    html=re.sub(r'<script\b[^>]*>[\s\S]*?</script>','',html,flags=re.I)
    html=re.sub(r'<link\b[^>]*(?:/static/|rss-subscribe-enhancement)[^>]*>','',html,flags=re.I)
    html=html.replace('</head>','<link rel="stylesheet" href="/static/mikan115.css">\n<script type="module" src="/assets/mikan115-app.js"></script>\n</head>')
    (ROOT/'templates/index.html').write_text(html,encoding='utf-8')
    count=import_catalog(reference/'legacy-data/mikan.db')
    (ROOT/'SOURCE.json').write_text(json.dumps({'project':'mikan-115','source_snapshot':'2026-10-08T16:15:00Z','source_archive_unchanged':True,'original_frontend_sha256':digest,'public_catalog_rows':count,'copied_private_accounts':False,'copied_legacy_credentials':False,'live_115_login_performed':False,'frontend_original_development_sources_found':False},indent=2),encoding='utf-8')
    print('REFERENCE_IMPORT_COMPLETE',flush=True)

if __name__=='__main__':main()
