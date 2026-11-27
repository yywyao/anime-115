"""Optional read-only Sonarr episode verification. No download/import commands."""
import json,re,urllib.parse
from .errors import AppError,ProviderError
from .http_client import HTTP

class Sonarr:
    def __init__(self,store,http=None):self.store=store;self.http=http or HTTP(timeout=5)
    def config(self):
        value=self.store.setting('sonarr_credentials')
        return json.loads(self.store.decrypt(value)) if value else {}
    def status(self):
        config=self.config()
        return {'configured':bool(config),'url':config.get('url',''),'api_version':config.get('api_version','v3')}
    def call(self,config,path,query=None):
        try:return self.http.json('GET',config['url']+'/api/'+config['api_version']+path+('?' + urllib.parse.urlencode(query) if query else ''),headers={'X-Api-Key':config['api_key']})
        except AppError:raise ProviderError('SONARR_UNAVAILABLE','Sonarr 连接失败，请检查地址和 API Key')
    def configure(self,body):
        if set(body)-{'url','api_key','api_version','clear'}:raise AppError('INVALID_SONARR','Sonarr 设置无效')
        if body.get('clear') is True:self.store.set_setting('sonarr_credentials','');return self.status()
        url=str(body.get('url') or '').strip().rstrip('/');version=body.get('api_version','v3')
        try:p=urllib.parse.urlsplit(url);p.port
        except ValueError:raise AppError('INVALID_SONARR','Sonarr 地址无效')
        if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment or '..' in p.path or any(ord(c)<33 for c in url) or version not in {'v3','v5'}:raise AppError('INVALID_SONARR','Sonarr 地址或 API 版本无效')
        old=self.config();key=body.get('api_key') or (old.get('api_key') if old.get('url')==url else '')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',key):raise AppError('INVALID_SONARR','请填写 API Key')
        config={'url':url,'api_key':key,'api_version':version};data=self.call(config,'/system/status')
        if data.get('appName')!='Sonarr' or not data.get('version'):raise ProviderError('SONARR_INVALID','目标服务不是 Sonarr')
        self.store.set_setting('sonarr_credentials',self.store.encrypt(json.dumps(config)));return self.status()
    def verify(self,title,work,season,episode):
        config=self.config()
        if not config:return None
        tvdb=(work.get('provider_ids') or {}).get('tvdb')
        if not str(tvdb or '').isdigit():return None
        parsed=self.call(config,'/parse',{'title':title});series=parsed.get('series') or {};info=parsed.get('parsedEpisodeInfo') or {}
        if str(series.get('tvdbId'))!=str(tvdb):raise AppError('SONARR_WORK_MISMATCH','Sonarr 作品标识不一致，已保留原名',409)
        episodes=info.get('episodeNumbers') or []
        if info.get('seasonNumber')!=season or episodes!=[episode]:raise AppError('SONARR_EPISODE_MISMATCH','Sonarr 集号不一致，已保留原名',409)
        return {'source':'Sonarr','series_id':series.get('id')}
