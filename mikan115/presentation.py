"""Language and title presentation rules shared by the catalog and TMDB."""
from functools import lru_cache
import html
import re

from opencc import OpenCC


_SIMPLIFIED = OpenCC('t2s')
_HAN = re.compile(r'[\u3400-\u9fff]')
_KANA = re.compile(r'[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fd-\u30ff\uff66-\uff6f\uff71-\uff9d]')
_FIRST = r'(?:第\s*[一1１]\s*[季期]|Season\s+1|1st\s+Season)'
_SEPARATOR = r'\s*(?:[·・:：/\-—]\s*)?'


@lru_cache(maxsize=8192)
def simplified(text):
    return _SIMPLIFIED.convert(text.strip())


def chinese(text, *, overview=False):
    """Accept Chinese source text; script alone does not translate Japanese or English."""
    if not isinstance(text, str):
        return ''
    plain = re.sub(r'<[^>]*>', '', re.sub(r'<br\s*/?\s*>','\n',html.unescape(text),flags=re.I))
    def is_chinese(part):
        han=len(_HAN.findall(part))
        kana=len(_KANA.findall(part))
        if not han or (kana and (not overview or kana*12>han)):return False
        return not overview or han*4>=sum(c.isalnum() for c in part)
    if is_chinese(plain):return simplified(plain if overview else text)
    if overview:
        # Bangumi may combine its Chinese translation and Japanese original in one field.
        paragraphs=[part.strip() for part in re.split(r'[\r\n]+',plain) if part.strip()]
        translated=[part for part in paragraphs if is_chinese(part) and len(_HAN.findall(part))>=4]
        if translated:return simplified('\n\n'.join(translated))
    return ''


def without_first_season(title):
    # An episode can retain its explicit number without spelling out season 1.
    title = re.sub(_FIRST + r'\s*(?=第\s*\d+\s*集\s*$)', '', title, flags=re.I)
    shortened = re.sub(_SEPARATOR + r'[（(【\[]?' + _FIRST + r'[）)】\]]?\s*$', '', title, flags=re.I)
    return shortened.strip() or title
