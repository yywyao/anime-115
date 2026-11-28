"""Admission for a user-confirmed work binding; fuzzy titles never bind IDs."""
import re
import unicodedata
from opencc import OpenCC

_simplify=OpenCC('t2s')


def normalized(value):
    text=_simplify.convert(unicodedata.normalize('NFKC',str(value or ''))).casefold()
    return ''.join(c for c in text if c.isalnum())


def aliases(work):
    values=[work.get(key) for key in ('title','title_original','series_title','title_zh','title_english','homeTitle')]
    for key in ('aliases','synonyms','alternative_titles'):
        raw=work.get(key) or []
        if isinstance(raw,list):values.extend(value.get('title') or value.get('name') if isinstance(value,dict) else value for value in raw)
    return {normalized(v) for v in values if isinstance(v,str) and normalized(v)}


def title_parts(title):
    text=unicodedata.normalize('NFKC',str(title or '')).replace('–','-').replace('—','-')
    text=re.sub(r'^\s*[\[【][^\]】]+[\]】]\s*','',text)
    stop=re.search(r'(?i)\bS\d{1,2}[ ._-]*E\d|(?:\s-\s*|[\[【])\d{1,4}(?=v\d|[\]】\s_.(]|$)|第\s*\d+\s*[话話集]|\b(?:episode|ep)[ ._-]*\d',text)
    if stop:text=text[:stop.start()]
    text=re.sub(r'[\[【][^\]】]*[\]】]',' ',text)
    parts=re.split(r'[/\\|]',text)
    stripped=[re.sub(r'(?i)(?:第\s*[零〇一二三四五六七八九十\d]{1,3}\s*季|\bseason[ ._-]*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)[ ._-]+season\b|\bS\d{1,2}\b)','',part) for part in parts]
    return {normalized(part) for part in [*parts,*stripped] if normalized(part)}


def bound_reason(item,context,catalog):
    """An explicit binding plus exact alias/season admits a release; no ID inference."""
    work=context['work'];release=item['release']
    if release['season'] is not None and context['season'] is not None and release['season']!=context['season']:return '季度不符'
    if '_binding' not in context:
        scope=catalog.tmdb_scope(work);known={n for n in work.get('known_seasons') or [] if type(n) is int and n>0};others=[]
        if scope and scope[0]=='tv':
            for row in catalog.seed_rows("WHERE CAST(json_extract(payload,'$.tmdb_id') AS TEXT)=?",(str(scope[1]),),1000):
                other=catalog.tmdb_scope(row)
                if other and other[:2]==scope[:2] and other[2]!=scope[2]:others.append(row);known.add(other[2])
        if context['season'] and context['season']>1:known.add(context['season'])
        context['_binding']={'aliases':aliases(work),'multi':len(known)>1 or any(n>1 for n in known),
            'ambiguous':(set().union(*(aliases(row) for row in others)) if others else set())|{normalized(work.get('series_title')),normalized(work.get('title_original'))}}
    binding=context['_binding'];matched=title_parts(item['title'])&binding['aliases']
    if not matched:return '作品不匹配'
    # Parent aliases shared by multiple seasons cannot identify an unnumbered
    # sequel. Distinct, confirmed arc titles remain useful admission labels.
    if release['season'] is None and binding['multi'] and matched<=binding['ambiguous']:return '季号未确认'
    return ''
