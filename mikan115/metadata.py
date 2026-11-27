"""Server-side TMDB configuration and public metadata; credentials never leave the server."""
import json
import re
import time
import urllib.parse

from .errors import AppError,ProviderError
from .http_client import HTTP

class Metadata:
    def __init__(self,store,http=None,base='https://api.themoviedb.org/3'):
        self.store=store;self.http=http or HTTP(timeout=5);self.base=base.rstrip('/')

    def credential(self):
        value=self.store.setting('tmdb_credentials')
        return self.store.decrypt(value) if value else ''

    def status(self):
        return {'tmdb_configured':bool(self.store.setting('tmdb_credentials')),
                'updated_at':self.store.setting('tmdb_credentials_updated_at')}

    def call(self,path,query=None,*,credential=None):
        value=self.credential() if credential is None else credential
        if not value:raise AppError('TMDB_NOT_CONFIGURED','请先填写 TMDB API',409)
        if not path.startswith('/') or '..' in path:raise AppError('INVALID_METADATA_PATH','请求无效')
        args=dict(query or {});headers={}
        if re.fullmatch(r'[a-fA-F0-9]{32}',value):args['api_key']=value
        else:headers['Authorization']='Bearer '+value
        url=self.base+path+('?' + urllib.parse.urlencode(args) if args else '')
        try:result=self.http.json('GET',url,headers=headers)
        except AppError:raise ProviderError('TMDB_UNAVAILABLE','TMDB 连接失败，请检查 API 后重试')
        if result.get('success') is False:raise ProviderError('TMDB_AUTH_FAILED','TMDB API 无效')
        return result

    def configure(self,value,*,clear=False):
        if clear:
            self.store.set_setting('tmdb_credentials','');self.store.set_setting('tmdb_credentials_updated_at',str(time.time()))
            return self.status()
        if not isinstance(value,str):raise AppError('INVALID_TMDB_CREDENTIAL','TMDB API 格式无效')
        value=value.strip()
        if not value:return self.status()
        if len(value)>4096 or any(ord(x)<33 or ord(x)==127 for x in value):
            raise AppError('INVALID_TMDB_CREDENTIAL','TMDB API 格式无效')
        if not re.fullmatch(r'[a-fA-F0-9]{32}',value) and not re.fullmatch(r'[A-Za-z0-9_\-.]{32,4096}',value):
            raise AppError('INVALID_TMDB_CREDENTIAL','TMDB API 格式无效')
        data=self.call('/authentication',credential=value)
        if data.get('success') is not True:raise ProviderError('TMDB_AUTH_FAILED','TMDB API 无效')
        self.store.set_setting('tmdb_credentials',self.store.encrypt(value))
        self.store.set_setting('tmdb_credentials_updated_at',str(time.time()))
        return self.status()

    @staticmethod
    def image(path,size='w500'):
        return 'https://image.tmdb.org/t/p/'+size+path if isinstance(path,str) and path.startswith('/') else ''

    @staticmethod
    def artwork_url(value,size='original'):
        if not isinstance(value,str):return ''
        url=urllib.parse.urlsplit(value)
        path=url.path
        if url.scheme or url.netloc:
            if url.scheme!='https' or url.netloc!='image.tmdb.org' or url.query or url.fragment:return ''
            match=re.fullmatch(r'/t/p/[^/]+(/[^/]+\.(?:jpg|jpeg|png|webp|svg))',path,re.I)
            path=match[1] if match else ''
        if not re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp|svg)',path,re.I):return ''
        return Metadata.image(path,size)

    @classmethod
    def select_artwork(cls,data,*,legacy_backdrop=''):
        """Keep text-free landscape backdrops and explicitly Japanese title logos."""
        images=data.get('images') or data
        def language(row):return row.get('iso_639_1',row.get('language'))
        def rank(row):
            def number(key):
                try:return float(row.get(key) or 0)
                except (TypeError,ValueError):return 0
            return (number('width')>=1280,number('vote_average'),number('vote_count'),number('width'))
        backdrops=[]
        for row in sorted((r for r in images.get('backdrops',[]) if isinstance(r,dict)),key=rank,reverse=True):
            try:landscape=float(row.get('width') or 0)>float(row.get('height') or 0)>0 and float(row.get('aspect_ratio') or float(row['width'])/float(row['height']))>=1.4
            except (TypeError,ValueError,KeyError,ZeroDivisionError):landscape=False
            if language(row) not in {None,'','null'} or not landscape:continue
            url=cls.artwork_url(row.get('file_path') or row.get('url'),'w1280')
            if url and url not in backdrops:backdrops.append(url)
        # A pre-existing TMDB backdrop is already a backdrop, never a poster.
        # Do not override an explicit gallery result that has no neutral image.
        if 'backdrops' not in images:
            url=cls.artwork_url(legacy_backdrop,'w1280')
            if url:backdrops.append(url)
        logos=[]
        for row in sorted((r for r in images.get('logos',[]) if isinstance(r,dict)),key=rank,reverse=True):
            if language(row)!='ja':continue
            url=cls.artwork_url(row.get('file_path') or row.get('url'))
            if url and url not in logos:logos.append(url)
        return {'backdrops':backdrops[:5],'logos':logos[:5],'logo_language':'ja' if logos else None}

    def artwork(self,media_type,ident):
        kind='movie' if media_type=='movie' else 'tv'
        if not str(ident).isdigit():raise AppError('INVALID_PROVIDER_ID','作品不存在',404)
        images=self.call('/'+kind+'/'+str(ident)+'/images',{'language':'ja','include_image_language':'ja,null'})
        return self.select_artwork(images)

    def title(self,media_type,ident,season=None):
        kind='movie' if media_type=='movie' else 'tv'
        if not str(ident).isdigit():raise AppError('INVALID_PROVIDER_ID','作品不存在',404)
        data=self.call('/'+kind+'/'+str(ident),{'language':'zh-CN','append_to_response':'credits'})
        result={'tmdb_id':int(ident),'media_type':'movie' if kind=='movie' else 'series',
                'title':data.get('name') or data.get('title') or '',
                'title_original':data.get('original_name') or data.get('original_title') or '',
                'overview':data.get('overview') or '',
                'poster_url':self.image(data.get('poster_path')),
                'backdrop_url':self.image(data.get('backdrop_path'),'w1280'),
                'year':int((data.get('first_air_date') or data.get('release_date') or '0')[:4] or 0) or None,
                'ratings':{'tmdb':data.get('vote_average')} if data.get('vote_count') and data.get('vote_average') else {},
                'vote_count':data.get('vote_count') or 0,
                'credits':{group:[{'id':p.get('id'),'name':p.get('name') or '',
                    'character':p.get('character') or p.get('job') or '',
                    'profile_url':self.image(p.get('profile_path'),'w185')}
                    for p in (data.get('credits') or {}).get(group,[])[:40]] for group in ['cast','crew']},
                'genres':[g['name'] for g in data.get('genres',[]) if g.get('name')],
                'seasons':[],'episodes':[]}
        if kind=='movie':return result
        selected=int(1 if season is None else season)
        known={int(s['season_number']):s for s in data.get('seasons',[]) if s.get('season_number') is not None}
        if selected not in known:
            # A parent's first season must not replace a missing second season.
            if selected>1:result.update(poster_url='',backdrop_url='',overview='',year=None)
            return result
        details=self.call('/tv/'+str(ident)+'/season/'+str(selected),{'language':'zh-CN'})
        result['seasons']=[{'season_number':selected,'name':details.get('name') or f'第 {selected} 季',
                            'air_date':details.get('air_date'),'episode_count':len(details.get('episodes') or []),
                            'poster_url':self.image(details.get('poster_path')),'provider':'TMDB'}]
        result['defaultSeason']=selected
        if selected>1:
            result['poster_url']=self.image(details.get('poster_path'))
            result['overview']=details.get('overview') or ''
            result['year']=int((details.get('air_date') or '0')[:4] or 0) or None
        result['episodes']=[{'id':e.get('id'),'season_number':selected,'episode_number':e.get('episode_number'),
            'title':e.get('name') or '', 'overview':e.get('overview') or '', 'air_date':e.get('air_date'),
            'still_url':self.image(e.get('still_path'),'w780'),'runtime':e.get('runtime')}
            for e in details.get('episodes',[]) if e.get('episode_number')]
        return result
