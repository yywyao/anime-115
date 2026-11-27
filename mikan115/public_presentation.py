"""Credential-free Bangumi synopsis and cover lookup with safe caching."""
import json
import re
import urllib.parse

from .errors import ProviderError
from .http_client import HTTP
from .presentation import chinese


class PublicOverview:
    """Fetch one exact public subject and retain good stale presentation data."""

    endpoint = 'https://api.bgm.tv/v0/subjects/'
    poster_hosts = {'lain.bgm.tv', 'lain.bangumi.tv'}
    poster_path = re.compile(r'^/(?:r/[1-9][0-9]*/)?pic/cover(?:/[A-Za-z0-9_-]+)+\.(?:jpg|jpeg|png|webp)$', re.I)

    def __init__(self, store, http=None, *, ttl=86400, retry_ttl=900):
        self.store = store
        self.http = http or HTTP(timeout=8)
        self.ttl = int(ttl)
        self.retry_ttl = int(retry_ttl)

    @staticmethod
    def valid_id(value):
        text=str(value or '')
        return text if text.isdigit() and int(text)>0 else ''

    @classmethod
    def cache_key(cls, bangumi_id):
        ident=cls.valid_id(bangumi_id)
        return 'bangumi:overview-zh:'+ident if ident else ''

    @classmethod
    def poster_key(cls, bangumi_id):
        ident=cls.valid_id(bangumi_id)
        return 'bangumi:poster:'+ident if ident else ''

    @classmethod
    def retry_key(cls, bangumi_id):
        ident=cls.valid_id(bangumi_id)
        return 'bangumi:presentation-retry:v2:'+ident if ident else ''

    @classmethod
    def poster_url(cls, value):
        if not isinstance(value,str):return ''
        try:url=urllib.parse.urlsplit(value)
        except ValueError:return ''
        if url.scheme!='https' or url.hostname not in cls.poster_hosts or url.username or url.password or url.query or url.fragment:return ''
        try:
            if url.port is not None:return ''
        except ValueError:return ''
        if not cls.poster_path.fullmatch(url.path):return ''
        if url.path.rsplit('/',1)[-1].rsplit('.',1)[0].lower()=='0':return ''
        return value

    def cached(self, bangumi_id, *, stale=True):
        key=self.cache_key(bangumi_id)
        if not key:return ''
        value=self.store.cache_get(key,stale=stale)
        return value if isinstance(value,str) else ''

    def cached_poster(self, bangumi_id, *, stale=True):
        key=self.poster_key(bangumi_id)
        if not key:return ''
        return self.poster_url(self.store.cache_get(key,stale=stale))

    def cached_posters(self,bangumi_ids):
        keys={self.poster_key(ident):self.valid_id(ident) for ident in bangumi_ids if self.valid_id(ident)}
        result={}
        names=list(keys)
        for offset in range(0,len(names),500):
            batch=names[offset:offset+500]
            rows=self.store.all('SELECT key,payload FROM catalog_cache WHERE key IN ('+
                                ','.join('?' for _ in batch)+')',batch)
            for row in rows:
                url=self.poster_url(json.loads(row['payload']))
                if url:result[keys[row['key']]]=url
        return result

    def should_refresh(self, bangumi_id, *, need_overview=True, need_poster=False):
        ident=self.valid_id(bangumi_id)
        if not ident or not (need_overview or need_poster):return False
        if self.store.cache_get(self.retry_key(ident)) is not None:return False
        missing_overview=need_overview and not self.cached(ident,stale=False)
        missing_poster=need_poster and not self.cached_poster(ident,stale=False)
        return bool(missing_overview or missing_poster)

    def fetch(self, bangumi_id):
        ident=self.valid_id(bangumi_id)
        if not ident:return {'overview':'','poster_url':''}
        data=self.http.json('GET',self.endpoint+ident,headers={'User-Agent':'Mikan115/1.0 (public catalog)'})
        if str(data.get('id'))!=ident or data.get('type') not in (None,2):
            raise ProviderError('BANGUMI_ID_MISMATCH','Bangumi 作品不一致，请重试')
        images=data.get('images') or {}
        poster=''
        for key in ('large','medium','common','small','grid'):
            poster=self.poster_url(images.get(key))
            if poster:break
        return {'overview':chinese(data.get('summary'),overview=True),'poster_url':poster}

    def refresh(self, bangumi_id, *, need_overview=True, need_poster=False):
        """Fetch once unless rate-limited, independently retaining each field."""
        ident=self.valid_id(bangumi_id)
        if not ident:return {'overview':'','poster_url':''} if need_poster else ''
        old_overview=self.cached(ident,stale=True)
        old_poster=self.cached_poster(ident,stale=True)
        if not self.should_refresh(ident,need_overview=need_overview,need_poster=need_poster):
            return {'overview':old_overview,'poster_url':old_poster} if need_poster else old_overview
        try:result=self.fetch(ident)
        except ProviderError:
            self.store.cache_set(self.retry_key(ident),True,self.retry_ttl)
            return {'overview':old_overview,'poster_url':old_poster} if need_poster else old_overview
        unavailable=False
        overview=result['overview']
        if overview:self.store.cache_set(self.cache_key(ident),overview,self.ttl)
        elif old_overview:unavailable=True
        else:self.store.cache_set(self.cache_key(ident),'',self.retry_ttl);unavailable=True
        poster=result['poster_url']
        if poster:self.store.cache_set(self.poster_key(ident),poster,self.ttl)
        elif old_poster:unavailable=True
        else:self.store.cache_set(self.poster_key(ident),'',self.retry_ttl);unavailable=True
        if unavailable:self.store.cache_set(self.retry_key(ident),True,self.retry_ttl)
        final_overview=overview or old_overview
        final_poster=poster or old_poster
        return {'overview':final_overview,'poster_url':final_poster} if need_poster else final_overview
