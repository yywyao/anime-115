"""Allowlisted anime feed adapters. No arbitrary feed or torrent URL proxy."""
import hashlib
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from .errors import AppError,ProviderError
from .http_client import HTTP
from .workflow import normalize_magnet
from . import releases


@dataclass(frozen=True)
class FeedSource:
    provider:str
    url:str
    kind:str='external'
    mikan_id:str=''
    group_id:str='0'

    @property
    def key(self):return 'feed:'+hashlib.sha256(self.url.encode()).hexdigest()[:24]


def local_name(tag):return tag.rsplit('}',1)[-1].casefold()


class FeedSources:
    names={'mikan':'蜜柑','dmhy':'动漫花园','nyaa':'Nyaa','acgrip':'ACG.RIP'}
    def __init__(self,config,catalog,http=None):
        self.config,self.catalog=config,catalog;self.http=http or HTTP(timeout=12)

    @staticmethod
    def _query(parsed,allowed):
        query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
        if set(query)-set(allowed) or any(len(values)!=1 or len(values[0])>1024 or any(ord(c)<32 for c in values[0]) for values in query.values()):
            raise AppError('INVALID_FEED','RSS 参数无效')
        return {key:values[0] for key,values in sorted(query.items())}

    def normalize(self,raw):
        if not isinstance(raw,str) or len(raw)>4096 or any(ord(c)<32 for c in raw):raise AppError('INVALID_FEED','RSS 链接无效')
        try:p=urllib.parse.urlsplit(raw.strip());port=p.port
        except ValueError:raise AppError('INVALID_FEED','RSS 链接无效')
        origin=urllib.parse.urlsplit(self.config.mikan_url)
        if p.scheme not in {'https','http'} or p.username or p.password or '\\' in raw or '..' in urllib.parse.unquote(p.path).split('/'):
            raise AppError('INVALID_FEED','RSS 链接无效')
        if p.hostname in {origin.hostname,'mikanani.me','mikanime.tv'}:
            if port and p.netloc!=origin.netloc:raise AppError('INVALID_FEED','RSS 端口无效')
            base=self.config.mikan_url.rstrip('/');path=p.path
            page=re.fullmatch(r'/Home/Bangumi/(\d{1,12})',path)
            if page:
                q=self._query(p,[]);q={'bangumiId':page[1]}
                if p.fragment:
                    if not p.fragment.isdigit():raise AppError('INVALID_FEED','字幕组链接无效')
                    q['subgroupid']=p.fragment
                path='/RSS/Bangumi'
            elif p.fragment:raise AppError('INVALID_FEED','RSS 链接无效')
            elif path=='/RSS/Bangumi':q=self._query(p,['bangumiId','subgroupid'])
            elif path=='/RSS/MyBangumi':
                q=self._query(p,['token'])
                if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',q.get('token','')):raise AppError('INVALID_FEED','个人 RSS 链接无效')
                return FeedSource('mikan',base+path+'?'+urllib.parse.urlencode(q),'personal')
            elif path=='/RSS/Classic':
                q=self._query(p,[]);return FeedSource('mikan',base+path,'personal')
            else:raise AppError('INVALID_FEED','请使用蜜柑作品或 RSS 链接')
            bid=q.get('bangumiId','');group=q.get('subgroupid','0')
            if not re.fullmatch(r'\d{1,12}',bid) or not re.fullmatch(r'\d{1,12}',group):raise AppError('INVALID_FEED','作品或字幕组标识无效')
            q={'bangumiId':bid,**({'subgroupid':group} if group!='0' else {})}
            return FeedSource('mikan',base+'/RSS/Bangumi?'+urllib.parse.urlencode(q),'bangumi',bid,group)
        if port not in (None,443) or p.fragment:raise AppError('INVALID_FEED','RSS 地址无效')
        if p.hostname in {'share.dmhy.org','dmhy.org','www.dmhy.org'}:
            if p.path not in {'/topics/rss/rss.xml','/topics/list','/topics/list/','/'}:raise AppError('INVALID_FEED','请使用动漫花园搜索或 RSS 链接')
            q=self._query(p,['keyword','sort_id','team_id','order'])
            if any(key in q and not q[key].isdigit() for key in ('sort_id','team_id')):raise AppError('INVALID_FEED','动漫花园筛选无效')
            if q.get('order','date-desc') not in {'date-desc','date-asc'}:raise AppError('INVALID_FEED','排序参数无效')
            q.setdefault('sort_id','2');group='dmhy:'+q['team_id'] if q.get('team_id') not in (None,'','0') else '0'
            return FeedSource('dmhy','https://share.dmhy.org/topics/rss/rss.xml'+('?' + urllib.parse.urlencode(q) if q else ''),'external','',group)
        if p.hostname in {'nyaa.si','www.nyaa.si'}:
            if p.path not in {'','/'}:raise AppError('INVALID_FEED','请使用 Nyaa 搜索或 RSS 链接')
            q=self._query(p,['page','q','c','f','u'])
            if q.get('page','rss') not in {'rss','search'} or not re.fullmatch(r'\d_\d',q.get('c','1_0')) or q.get('f','0') not in {'0','1','2'}:raise AppError('INVALID_FEED','Nyaa 筛选无效')
            q.update(page='rss');q.setdefault('c','1_0');q.setdefault('f','0')
            return FeedSource('nyaa','https://nyaa.si/?'+urllib.parse.urlencode(q))
        if p.hostname in {'acg.rip','www.acg.rip'}:
            if p.path not in {'/','/.xml','/1.xml'}:raise AppError('INVALID_FEED','请使用 ACG.RIP 搜索或 RSS 链接')
            q=self._query(p,['term'])
            return FeedSource('acgrip','https://acg.rip/.xml'+('?' + urllib.parse.urlencode(q) if q else ''))
        raise AppError('UNSUPPORTED_FEED','暂不支持此 RSS 来源')

    def search(self,work,provider,group_id='0'):
        title=work.get('series_title') or work.get('homeTitle') or work.get('title') or ''
        bid=str(work.get('mikan_id') or work.get('bangumiId') or '')
        if provider=='mikan':
            if not bid.isdigit():raise AppError('MIKAN_SOURCE_REQUIRED','此作品尚未关联蜜柑，请选择其他来源',409)
            return self.normalize(self.config.mikan_url+'/RSS/Bangumi?'+urllib.parse.urlencode({'bangumiId':bid,**({'subgroupid':group_id} if group_id!='0' else {})}))
        if provider=='dmhy':return self.normalize('https://share.dmhy.org/topics/rss/rss.xml?'+urllib.parse.urlencode({'keyword':title,'sort_id':'2'}))
        if provider=='nyaa':
            names=work.get('aliases') or []
            title=next((name for name in names if isinstance(name,str) and re.fullmatch(r'[\x20-\x7e]+',name) and re.search('[A-Za-z]',name)),title)
            return self.normalize('https://nyaa.si/?'+urllib.parse.urlencode({'page':'rss','q':title,'c':'1_0','f':'0'}))
        if provider=='acgrip':return self.normalize('https://acg.rip/.xml?'+urllib.parse.urlencode({'term':title}))
        raise AppError('UNSUPPORTED_FEED','请选择 RSS 来源')

    def fetch(self,source):
        checked=self.normalize(source.url)
        if checked.provider!=source.provider:raise AppError('INVALID_FEED','RSS 来源已变化')
        if source.provider=='mikan':
            p=urllib.parse.urlsplit(source.url)
            return self.catalog.fetch(p.path+('?' + p.query if p.query else ''),binary=True)
        return self.http.bytes('GET',source.url,limit=4*1024*1024,headers={'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'})

    def torrent_url(self,source,url):
        try:p=urllib.parse.urlsplit(urllib.parse.urljoin(source.url,url));port=p.port
        except ValueError:return ''
        if p.username or p.password or p.fragment or p.query or p.scheme not in {'https','http'}:return ''
        origin=urllib.parse.urlsplit(self.config.mikan_url)
        if source.provider=='mikan' and p.netloc==origin.netloc and re.fullmatch(r'/Download/[A-Za-z0-9/_-]+\.torrent',p.path):return urllib.parse.urlunsplit(p)
        if port not in (None,443):return ''
        if source.provider=='nyaa' and p.hostname=='nyaa.si' and re.fullmatch(r'/download/\d+\.torrent',p.path):return 'https://nyaa.si'+p.path
        if source.provider=='acgrip' and p.hostname=='acg.rip' and re.fullmatch(r'/t/\d+(?:\.torrent)?',p.path):return 'https://acg.rip'+p.path
        if source.provider=='dmhy' and p.hostname=='btcache.me' and re.fullmatch(r'/torrent/[a-fA-F0-9]{40}\.torrent',p.path):return 'https://btcache.me'+p.path
        return ''

    def torrent(self,source,url):
        safe=self.torrent_url(source,url)
        if not safe:raise AppError('UNSAFE_TORRENT_SOURCE','种子来源未确认',403)
        if source.provider=='mikan':
            p=urllib.parse.urlsplit(safe);return self.catalog.fetch(p.path,binary=True)
        return self.http.bytes('GET',safe,limit=4*1024*1024,headers={'Accept':'application/x-bittorrent'})

    def entries(self,source,raw=None):
        raw=self.fetch(source) if raw is None else raw
        if len(raw)>4*1024*1024 or b'\0' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ProviderError('RSS_INVALID','RSS 内容无效')
        try:root=ET.fromstring(raw)
        except ET.ParseError:raise ProviderError('RSS_INVALID','RSS 格式不正确')
        nodes=[n for n in root.iter() if local_name(n.tag) in {'item','entry'}][:1000];out=[]
        origin=urllib.parse.urlsplit(self.config.mikan_url)
        for item in nodes:
            values={};urls=[]
            for node in item.iter():
                name=local_name(node.tag);text=html.unescape(''.join(node.itertext()).strip())
                if name in {'title','guid','id','link','pubdate','published','updated','infohash'}:values.setdefault(name,text)
                for attr in ('url','href'):
                    if node.attrib.get(attr):urls.append(html.unescape(node.attrib[attr]))
                if text.startswith('magnet:?'):urls.append(text)
                urls.extend(html.unescape(m) for m in re.findall(r'''(?:href|url)=["'](magnet:\?[^"']+)["']''',text,re.I))
            title=values.get('title','RSS 资源')[:500];magnets={}
            if re.fullmatch('[a-fA-F0-9]{40}',values.get('infohash','')):urls.append('magnet:?xt=urn:btih:'+values['infohash'])
            for url in urls:
                if url.startswith('magnet:?'):
                    try:key,magnet=normalize_magnet(url);magnets[key]=magnet
                    except AppError:pass
            if len(magnets)>1:continue
            magnet=next(iter(magnets.values()),'');torrent=next((safe for url in urls if (safe:=self.torrent_url(source,url))),'')
            if not magnet and not torrent:continue
            link=urllib.parse.urlsplit(values.get('link',''));episode=''
            if source.provider=='mikan' and (not link.netloc or link.netloc==origin.netloc) and re.fullmatch(r'/Home/Episode/[a-fA-F0-9]{40}',link.path):episode=link.path
            guid=values.get('guid') or values.get('id') or magnet or torrent
            identity=next(iter(magnets)) if magnets else torrent
            group=source.group_id if source.group_id!='0' else ''
            release=releases.parse(title,group)
            if source.provider!='mikan' and not group and release['group']:
                release['group_id']=source.provider+':'+hashlib.sha256(release['group'].casefold().encode()).hexdigest()[:16]
            published=None
            try:published=parsedate_to_datetime(values.get('pubdate') or values.get('published') or '').timestamp()
            except (TypeError,ValueError,OverflowError):pass
            resource_hash=episode.rsplit('/',1)[-1] if episode else next(iter(magnets),'')
            out.append({'key':hashlib.sha256(identity.encode()).hexdigest(),'legacy_key':hashlib.sha256(guid.encode()).hexdigest(),'source_key':source.key,
                'resource_hash':resource_hash,'infohash':next(iter(magnets),''),'title':title,'magnet':magnet,'torrent':torrent,'episode_path':episode,
                'bangumi_id':source.mikan_id,'provider':source.provider,'published_at':published,'release':release})
        return out
