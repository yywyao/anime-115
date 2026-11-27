"""Search tolerance never establishes work or season identity."""
import difflib
import re
import unicodedata

def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value)).casefold() if c.isalnum())

def strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,list):
        for item in value:yield from strings(item)
    elif isinstance(value,dict):
        for item in value.values():yield from strings(item)

def names(work):
    values=[]
    for field in ['title','tmdbTitle','title_zh','title_original','original_title','title_english','title_romaji','aliases','synonyms','titles']:
        values.extend(strings(work.get(field)))
    return tuple(dict.fromkeys(normalize(v) for v in values if v))[:80]

def score(query,target):
    if not query or not target:return 0
    if query==target:return 100
    if query in target:return 95 if target.startswith(query) else 90
    if len(query)<2 or len(target)>240:return 0
    # Ordered fragments support e.g. 冰城墙 and missing particles/spaces.
    it=iter(target)
    if all(char in it for char in query) and len(query)/len(target)>=.35:return 72+12*len(query)/len(target)
    if len(set(query)&set(target))/max(1,len(set(query)))<.5:return 0
    if max(len(query),len(target))>2.2*min(len(query),len(target)):return 0
    ratio=difflib.SequenceMatcher(None,query,target,autojunk=False).ratio()
    return ratio*85 if ratio>=.70 else 0

def build_index(works):
    return [(work,names(work)) for work in works if work.get('title')]

def search(index,term,limit=100):
    query=normalize(str(term).strip()[:100])
    if not query:return []
    matches=[]
    for work,aliases in index:
        value=max((score(query,alias) for alias in aliases),default=0)
        if value:matches.append((value,bool(work.get('mikan_id')),work))
    matches.sort(key=lambda item:(-item[0],-item[1],str(item[2].get('title'))))
    return [work for _,_,work in matches[:limit]]
