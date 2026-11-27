"""Conservative release labels and admission rules. Titles never identify a work."""
import re
import unicodedata
from .errors import AppError

VERSIONS={'chs':'简中','cht':'繁中','chs-ja':'简日','cht-ja':'繁日','chs-cht':'简繁','chs-cht-ja':'简繁日','zh-ja':'中日','ja':'日文','none':'无字幕','unknown':'未标注'}
DEFAULT_RULES={'group_ids':[],'versions':[],'resolutions':[],'include':[],'exclude':[],'dedupe':'episode','wait_minutes':0,'allow_batches':False,'min_episode':None,'max_episode':None,'rename':False,'season':None,'episode_offset':0,'episode_title':False}

def parse(title,group_id='',group_name=''):
    title=unicodedata.normalize('NFKC',str(title or ''))[:500];lower=title.casefold()
    lower=re.sub(r'(?<=[简簡繁中])[\s_/&+|、,·-]+(?=[简簡繁日中])','',lower)
    def has(pattern):return bool(re.search(pattern,lower,re.I))
    chs=has(r'简[体中日繁]|簡[体體中日繁]|简繁|簡繁|(?<![a-z])(?:chs|sc|gb|gb2312)(?![a-z])')
    cht=has(r'繁[体體中日]|简繁|簡繁|(?<![a-z])(?:cht|tc|big5)(?![a-z])')
    ja=has(r'(?:简|簡|繁|中)日|简繁日|簡繁日|日[语語文]|(?<![a-z])(?:jpn|jp|japanese)(?![a-z])')
    if has(r'(?:chs|cht|sc|tc)\s*[&+_/ -]\s*(?:jp|jpn)'):ja=True
    version='-'.join(k for k,v in [('chs',chs),('cht',cht),('ja',ja)] if v)
    if not chs and not cht and has(r'中日'):version='zh-ja'
    if version not in VERSIONS:version='unknown'
    if has(r'无字幕|無字幕|\bno[ ._-]*subs?(?:titles)?\b'):version='none'
    resolution=next((v for p,v in [(r'2160[pi]|\b4k\b','2160p'),(r'1080[pi]|1920\s*[x×]\s*1080','1080p'),(r'720[pi]|1280\s*[x×]\s*720','720p'),(r'480[pi]','480p')] if has(p)),'unknown')
    mode='external' if has(r'外挂|外掛|external') else 'soft' if has(r'内封|內封|softsub') else 'hard' if has(r'内嵌|內嵌|hardsub') else 'unknown'
    season=None;episodes=[];explicit=False
    match=re.search(r'\bS(\d{1,2})[ ._-]*E(\d{1,4})(?:[ ._-]*E(\d{1,4}))?',title,re.I)
    if match:
        season=int(match[1]);episodes=[int(match[2])]+([int(match[3])] if match[3] else []);explicit=True
    else:
        match=re.search(r'(?:第\s*|\b(?:episode|ep|e)[ ._-]*)(\d{1,4})(?:\s*[-~至]\s*(\d{1,4}))?\s*(?:集|话|話|\b)',title,re.I)
        if not match:match=re.search(r'(?:\s-\s*|[\[【])(\d{1,3})(?:\s*[-~至]\s*(\d{1,3}))?(?=v\d|[\]】\s_.(]|$)',title,re.I)
        if match:
            start=int(match[1]);end=int(match[2]) if match[2] else start
            if start<=end<=start+100:episodes=list(range(start,end+1))
    batch=len(episodes)>1 or has(r'全集|合集|合辑|合集|\b(?:batch|complete)\b|全\s*\d+\s*[话話集]')
    revision=re.search(r'(?:\b\d{1,3}|[\]】 ])v(\d)\b',lower)
    first=re.match(r'^[\[【]([^\]】]+)[\]】]',title)
    return {'group_id':str(group_id or ''),'group':str(group_name or (first[1] if first else ''))[:120],
        'version':version,'version_label':VERSIONS[version],'resolution':resolution,'subtitle_mode':mode,
        'season':season,'episodes':episodes,'explicit_season':explicit,'batch':batch,'revision':int(revision[1]) if revision else 1}

def rules(value=None):
    if value is None:value={}
    if not isinstance(value,dict) or set(value)-set(DEFAULT_RULES):raise AppError('INVALID_RULES','追新规则无效')
    out={**DEFAULT_RULES,**value}
    for key,limit in [('group_ids',100),('versions',10),('resolutions',5),('include',20),('exclude',20)]:
        values=out[key]
        if not isinstance(values,list) or len(values)>limit or any(not isinstance(v,str) or len(v)>100 for v in values):raise AppError('INVALID_RULES','筛选条件过长')
        out[key]=list(dict.fromkeys(v.strip() for v in values if v.strip()))
    if any(not re.fullmatch(r'\d+',v) for v in out['group_ids']):raise AppError('INVALID_GROUP','字幕组标识无效')
    if set(out['versions'])-set(VERSIONS) or set(out['resolutions'])-{'2160p','1080p','720p','480p','unknown'}:raise AppError('INVALID_RULES','字幕或分辨率无效')
    if out['dedupe'] not in {'episode','episode_version','hash'}:raise AppError('INVALID_RULES','去重方式无效')
    for key,low,high in [('wait_minutes',0,1440),('season',0,99),('min_episode',0,9999),('max_episode',0,9999),('episode_offset',-1000,1000)]:
        v=out[key]
        if v is None and key in {'season','min_episode','max_episode'}:continue
        if isinstance(v,bool) or not isinstance(v,int) or not low<=v<=high:raise AppError('INVALID_RULES','集数或等待时间无效')
    if out['min_episode'] is not None and out['max_episode'] is not None and out['min_episode']>out['max_episode']:raise AppError('INVALID_RULES','起始集数不能大于结束集数')
    for key in ['allow_batches','rename','episode_title']:
        if not isinstance(out[key],bool):raise AppError('INVALID_RULES','规则开关无效')
    return out

def admit(item,rule):
    r=item['release'];title=unicodedata.normalize('NFKC',item['title']).casefold()
    if rule['group_ids'] and r['group_id'] not in rule['group_ids']:return '字幕组不符'
    if rule['versions'] and r['version'] not in rule['versions']:return '字幕版本不符'
    if rule['resolutions'] and r['resolution'] not in rule['resolutions']:return '分辨率不符'
    if any(unicodedata.normalize('NFKC',v).casefold() not in title for v in rule['include']):return '缺少必含词'
    if any(unicodedata.normalize('NFKC',v).casefold() in title for v in rule['exclude']):return '命中排除词'
    if r['batch'] and not rule['allow_batches']:return '合集未启用'
    if rule['season'] is not None and r['season'] is not None and rule['season']!=r['season']:return '季度不符'
    episodes=normalized_episodes(r,rule)
    if rule['min_episode'] is not None and (not episodes or min(episodes)<rule['min_episode']):return '早于起始集数'
    if rule['max_episode'] is not None and (not episodes or max(episodes)>rule['max_episode']):return '超出结束集数'
    return ''

def priority(item,rule):
    r=item['release']
    def rank(values,v):return values.index(v) if v in values else len(values)
    return (rank(rule['group_ids'],r['group_id']),rank(rule['versions'],r['version']) if rule['versions'] else int(r['version']=='unknown'),rank(rule['resolutions'] or ['1080p','2160p','720p','480p','unknown'],r['resolution']),-r['revision'],item['key'])

def episode_keys(item,rule):
    r=item['release'];bid=item.get('bangumi_id')
    if not bid or not r['episodes'] or rule['dedupe']=='hash':return ['hash:'+item['key']]
    # A naming override must not create a new duplicate identity.
    season=r['season'] if r['season'] is not None else item.get('identity_season')
    # A Mikan bangumi id already scopes the season; no guessed TMDB identity.
    suffix=':'+r['version'] if rule['dedupe']=='episode_version' else ''
    return [f"mikan:{bid}:s{season if season is not None else 'source'}:e{n}{suffix}" for n in normalized_episodes(r,rule)]

def normalized_episodes(release,rule):
    offset=0 if release.get('explicit_season') else rule['episode_offset']
    return [episode+offset for episode in release.get('episodes',[])]
