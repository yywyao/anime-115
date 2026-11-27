"""Public-source cross references. Matching titles never establishes identity."""
import contextlib
import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

BANGUMI_DATA='https://raw.githubusercontent.com/bangumi-data/bangumi-data/master/dist/data.json'
FRIBB='https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json'
MANIFEST='https://github.com/open-ani/bangumi-tmdb/releases/latest/download/manifest.json'

class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in {'github.com','raw.githubusercontent.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}:
            raise ValueError('Public feed redirect rejected')
        return super().redirect_request(req,fp,code,msg,headers,url)

class PublicFeeds:
    def __init__(self,folder):self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
    def get(self,name,url,expected=None):
        path=self.folder/name
        # A pinned, checksum-verified release does not need downloading again.
        if expected and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:return json.loads(path.read_bytes())
        opener=urllib.request.build_opener(PublicRedirect())
        request=urllib.request.Request(url,headers={'User-Agent':'Mikan115/1.0 (public catalog sync)'})
        with opener.open(request,timeout=35) as response:raw=response.read(70*1024*1024+1)
        if len(raw)>70*1024*1024:raise ValueError('Public feed too large')
        if expected and hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Public feed checksum mismatch')
        value=json.loads(raw);temp=path.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(path)
        return value
    def load(self):
        manifest=self.get('mapping-manifest.json',MANIFEST)
        commit=manifest.get('sourceCommit','')
        if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('Invalid public release version')
        expected=manifest['files']['subjects.json']['sha256']
        subjects=self.get('mapping-subjects.json','https://github.com/open-ani/bangumi-tmdb/releases/download/data-'+commit+'/subjects.json',expected)
        if subjects.get('schemaVersion')!=1 or not isinstance(subjects.get('subjects'),list):raise ValueError('Invalid public mapping schema')
        bg=self.get('bgdata.json',BANGUMI_DATA);fribb=self.get('fribb.json',FRIBB)
        if not isinstance(bg.get('items'),list) or not isinstance(fribb,list):raise ValueError('Invalid public index schema')
        return bg,fribb,subjects,manifest

def identifier(value):
    return str(value) if str(value or '').isdigit() and int(value)>0 else ''

def valid_date(value):
    try:return datetime.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:return ''

def target(kind,ident,season=None):
    ident=identifier(ident)
    if kind not in {'tv','movie'} or not ident:return None
    if season is not None:
        try:season=int(season)
        except (ValueError,TypeError):return None
        if season<0:return None
    return {'type':kind,'id':int(ident),'season':season if kind=='tv' else None}

def unique_targets(values):
    return list({(v['type'],v['id'],v['season']):v for v in values if v}.values())

class CrossReferences:
    def __init__(self,bg,fribb,subjects):
        self.links={};self.details={};self.conflicts=[];self.bg_records=[]
        def register(provider,ident,record):
            if identifier(ident):self.links.setdefault((provider,str(ident)),[]).append(record)
        for item in bg.get('items',[]):
            ids={};targets=[]
            for site in item.get('sites',[]):
                provider={'aniList':'anilist','mal':'mal','bangumi':'bangumi'}.get(site.get('site'))
                if provider and identifier(site.get('id')):ids[provider]=str(site['id'])
                if site.get('site')=='tmdb':
                    match=re.fullmatch(r'(tv|movie)/(\d+)(?:/season/(\d+))?',str(site.get('id','')))
                    if match:targets.append(target(*match.groups()))
            aliases=[item.get('title','')]
            for v in (item.get('titleTranslate') or {}).values():aliases.extend(v if isinstance(v,list) else [v])
            record={'ids':ids,'aliases':aliases,'targets':unique_targets(targets),'date':item.get('begin') or '', 'source':'bangumi-data'}
            record['title']=next(iter((item.get('titleTranslate') or {}).get('zh-Hans') or []),item.get('title',''))
            self.bg_records.append(record)
            for p,ident in ids.items():register(p,ident,record)
        for row in fribb:
            ids={p:identifier(row.get(k)) for p,k in [('anilist','anilist_id'),('mal','mal_id')]};ids={p:v for p,v in ids.items() if v}
            t=row.get('themoviedb_id') or {};values=[]
            if isinstance(t,dict):
                if t.get('tv'):values.append(target('tv',t['tv'],(row.get('season') or {}).get('tmdb')))
                movies=t.get('movie') or []
                for ident in movies if isinstance(movies,list) else [movies]:values.append(target('movie',ident))
            record={'ids':ids,'aliases':[],'targets':unique_targets(values),'source':'Fribb/anime-lists'}
            for p,ident in ids.items():register(p,ident,record)
        for row in subjects.get('subjects',[]):
            bid=identifier(row.get('bangumiId'));values=[];episode_only=[]
            for t in row.get('targets',[]):
                # Episode-only specials cannot replace a subject's main season.
                if t.get('episode') is None:values.append(target(t.get('type'),t.get('id'),t.get('season')))
                else:episode_only.append(target(t.get('type'),t.get('id'),t.get('season')))
            record={'ids':{'bangumi':bid},'aliases':[],'targets':unique_targets(values or episode_only),'episode_scope':not values and bool(episode_only),'source':'open-ani/bangumi-tmdb'}
            if bid:register('bangumi',bid,record)
    def enrich(self,work):
        out=dict(work);out.pop('tmdb_link_conflict',None);ids={str(k).lower():str(v) for k,v in (work.get('provider_ids') or {}).items() if v}
        rows=[];visited=set()
        # Iterate links until no new explicit source id is discovered.
        for _ in range(3):
            proposed={}
            for pair in list(ids.items()):
                if pair in visited:continue
                visited.add(pair)
                for record in self.links.get(pair,[]):
                    # Reject a bridge that contradicts another known subject id.
                    if any(ids.get(p) and ids[p]!=ident for p,ident in record['ids'].items()):continue
                    rows.append(record)
                    for p,ident in record['ids'].items():proposed.setdefault(p,set()).add(ident)
            for p,values in proposed.items():
                if len(values)==1:ids.setdefault(p,next(iter(values)))
        original=re.fullmatch(r'catalog~(\d+)',str(work.get('id','')))
        corrected=bool(original and ids.get('anilist') and original[1]!=ids['anilist'])
        aliases=list(work.get('aliases') or []) if isinstance(work.get('aliases'),list) and not corrected else []
        for row in rows:aliases.extend(a for a in row['aliases'] if a)
        out['aliases']=list(dict.fromkeys(aliases))[:80];out['provider_ids']=ids
        if corrected:
            originals={r['aliases'][0] for r in rows if r['source']=='bangumi-data' and r['aliases']}
            if len(originals)==1:out['title_original']=next(iter(originals))
            out['identity_adjusted']=True
            out['ratings']={p:v for p,v in (out.get('ratings') or {}).items() if str((out.get('rating_details') or {}).get(p,{}).get('provider_id') or '')==str(ids.get(p) or '') and ids.get(p)}
        dates={valid_date(row.get('date')) for row in rows if valid_date(row.get('date'))}
        if len(dates)==1 and (not work.get('year') or corrected):
            date=next(iter(dates));out['air_date']=date;out['year']=int(date[:4]);out['release_season']=['冬','春','夏','秋'][(int(date[5:7])-1)//3]
        candidates=[]
        for row in rows:
            for t in row['targets']:candidates.append({**t,'source':row['source']})
        # A multi-film subject is not the first film in its collection.
        compound=[r for r in rows if r['source']=='open-ani/bangumi-tmdb' and len({(t['type'],t['id']) for t in r['targets']})>1]
        if compound and not (work.get('tmdb_id') or ids.get('tmdb')):
            out['tmdb_targets']=unique_targets(compound[0]['targets']);out['tmdb_mapping']={'status':'multiple_works','sources':['open-ani/bangumi-tmdb']};return out
        types={(c['type'],c['id']) for c in candidates}
        if len(types)>1:
            votes={pair:{c['source'] for c in candidates if (c['type'],c['id'])==pair} for pair in types}
            agreed=[pair for pair,sources in votes.items() if len(sources)>=2]
            if len(agreed)==1:
                chosen=agreed[0];candidates=[c for c in candidates if (c['type'],c['id'])==chosen];types={chosen}
                out['tmdb_link_resolution']='cross_source_agreement'
        if len(types)==1:
            kind,ident=next(iter(types));existing=work.get('tmdb_id') or ids.get('tmdb')
            if existing and str(existing)!=str(ident):
                out['tmdb_link_conflict']=True;return out
            seasons={c['season'] for c in candidates if c['season'] is not None}
            out['tmdb_id']=ident;out['provider_ids']={**ids,'tmdb':str(ident)}
            out['media_type']='movie' if kind=='movie' else 'series'
            out['tmdb_mapping']={'type':kind,'id':ident,'season':next(iter(seasons)) if len(seasons)==1 else None,'sources':sorted({c['source'] for c in candidates}),'status':'season_ambiguous' if len(seasons)>1 else 'work_only' if not seasons and kind=='tv' else 'linked'}
            if any(r.get('episode_scope') for r in rows):out['tmdb_mapping']['status']='episode_scope'
            if len(seasons)==1:out['tmdb_season']=next(iter(seasons))
        elif len(types)>1:out['tmdb_link_conflict']=True
        return out

    def missing_works(self,works,first=2012,last=None):
        known={(p,str(v)) for w in works for p,v in (w.get('provider_ids') or {}).items()}
        result=[];last=last or time.gmtime().tm_year+1
        for row in self.bg_records:
            date=valid_date(row.get('date'))
            if not date or not first<=int(date[:4])<=last:continue
            if not row['ids'] or any((p,v) in known for p,v in row['ids'].items()):continue
            p,ident=next(iter(row['ids'].items()));wid='public:'+p+':'+ident
            work={'id':wid,'canonical_id':wid,'title':row['title'],'title_original':row['aliases'][0],'aliases':row['aliases'],'provider_ids':row['ids'],'year':int(date[:4]),'air_date':date[:10],'release_season':['冬','春','夏','秋'][(int(date[5:7])-1)//3]}
            result.append(self.enrich(work));known.update(row['ids'].items())
        return result

def update_cross_references(catalog,feeds):
    bg,fribb,subjects,manifest=feeds.load();index=CrossReferences(bg,fribb,subjects)
    works=catalog.all_works();updated=[index.enrich(w) for w in works];updated+=index.missing_works(updated)
    catalog.save_works(updated);catalog._references=index
    stats={'works':len(updated),'tmdb_before':sum(bool(w.get('tmdb_id')) for w in works),'tmdb_after':sum(bool(w.get('tmdb_id')) for w in updated),'conflicts':sum(bool(w.get('tmdb_link_conflict')) for w in updated),'source_commit':manifest['sourceCommit'],'updated_at':time.time()}
    catalog.sync_result('references',stats,86400)
    return stats
