"""Explicit work/season selection for following; search never confirms identity."""
from .errors import AppError
from .inventory import work_scope


class TrackingCatalog:
    def __init__(self,catalog,metadata):self.catalog,self.metadata=catalog,metadata

    def public(self,work):
        return {**{key:work.get(key) for key in ('id','canonical_id','title','homeTitle','series_title','title_original','year','mikan_id','tmdb_id','tmdb_season','tmdb_mapping')},
                'scope_key':work_scope(self.catalog,work)}

    def search(self,query,live=False):
        query=str(query).strip()[:120]
        if not query:return {'results':[],'tmdb_configured':self.metadata.status()['tmdb_configured']}
        results=[];seen=set()
        for result in self.catalog.search(query)['results'][:80]:
            work=self.catalog.work(result.get('canonical_id') or result.get('id') or result.get('mikan_id'))
            scope=work_scope(self.catalog,work)
            if scope and scope not in seen:results.append(self.public(work));seen.add(scope)
        if live and self.metadata.status()['tmdb_configured']:
            data=self.metadata.call('/search/tv',{'query':query,'language':'zh-CN','include_adult':'false'})
            for row in data.get('results',[])[:20]:
                if type(row.get('id')) is not int:continue
                results.append({'title':row.get('name') or row.get('original_name') or '',
                    'title_original':row.get('original_name') or '', 'year':str(row.get('first_air_date') or '')[:4],
                    'tmdb_id':row['id'],'needs_season':True})
        return {'results':results,'tmdb_configured':self.metadata.status()['tmdb_configured']}

    def choose(self,body):
        if set(body)-{'tmdb_id','season'} or not str(body.get('tmdb_id') or '').isdigit():raise AppError('INVALID_PROVIDER_ID','请选择 TMDB 作品')
        ident=str(body['tmdb_id']);season=body.get('season')
        if season is None:
            data=self.metadata.call('/tv/'+ident,{'language':'zh-CN'})
            if str(data.get('id'))!=ident:raise AppError('TMDB_ID_MISMATCH','TMDB 作品不一致')
            return {'title':data.get('name') or data.get('original_name') or '', 'seasons':[
                {key:row.get(key) for key in ('season_number','name','air_date','episode_count')}
                for row in data.get('seasons',[]) if type(row.get('season_number')) is int and row['season_number']>0]}
        if type(season) is not int or not 0<=season<=99:raise AppError('INVALID_SEASON','请选择季度')
        existing=self.catalog.tmdb_work('tv',ident,season)
        if existing:return {'work':self.public(existing)}
        details=self.metadata.title('tv',ident,season)
        if season not in details.get('known_seasons',[]):raise AppError('INVALID_SEASON','未找到该季度')
        parent=self.metadata.call('/tv/'+ident,{'language':'zh-CN','append_to_response':'alternative_titles,external_ids'})
        if str(parent.get('id'))!=ident:raise AppError('TMDB_ID_MISMATCH','TMDB 作品不一致')
        key=f'tmdb:tv:{ident}:s{season}'
        title=details.get('tmdbTitle') or details['title']
        aliases=[parent.get('name'),parent.get('original_name')]+[row.get('title') for row in (parent.get('alternative_titles') or {}).get('results',[])]
        work={**details,'id':key,'canonical_id':key,'title':title,'homeTitle':title,'series_title':parent.get('name') or parent.get('original_name') or title,
            'aliases':[name for name in aliases if name], 'tmdb_season':season,
            'tmdb_mapping':{'status':'linked','type':'tv','id':int(ident),'season':season,'source':'user-confirmed'},
            'provider_ids':{'tmdb':ident,**({'tvdb':str(parent['external_ids']['tvdb_id'])} if (parent.get('external_ids') or {}).get('tvdb_id') else {})}}
        self.catalog.save_works([work]);return {'work':self.public(work)}
