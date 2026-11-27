import hashlib
import urllib.parse

from .errors import AppError


def torrent_magnet(raw,title=''):
    """Hash the exact bencoded info bytes, never a reserialized dictionary."""
    if not isinstance(raw,bytes) or not 2<=len(raw)<=4*1024*1024:raise AppError('INVALID_TORRENT','种子文件大小或格式无效')
    pos=0;nodes=0;info_range=None
    def parse(depth=0):
        nonlocal pos,nodes,info_range
        nodes+=1
        if depth>64 or nodes>200000 or pos>=len(raw):raise ValueError('limits')
        ch=raw[pos:pos+1]
        if ch==b'i':
            pos+=1;end=raw.index(b'e',pos);v=raw[pos:end]
            if not v or v==b'-0' or (v.startswith(b'0') and len(v)>1):raise ValueError('integer')
            int(v);pos=end+1;return
        if ch in (b'l',b'd'):
            dictionary=ch==b'd';pos+=1;keys=set()
            while pos<len(raw) and raw[pos:pos+1]!=b'e':
                if dictionary:
                    key=string()
                    if key in keys:raise ValueError('duplicate key')
                    keys.add(key);start=pos;parse(depth+1)
                    if depth==0 and key==b'info':
                        if raw[start:start+1]!=b'd':raise ValueError('info dictionary required')
                        info_range=(start,pos)
                else:parse(depth+1)
            if pos>=len(raw):raise ValueError('truncated')
            pos+=1;return
        string()
    def string():
        nonlocal pos
        colon=raw.index(b':',pos);digits=raw[pos:colon]
        if not digits.isdigit() or (digits.startswith(b'0') and len(digits)>1):raise ValueError('string length')
        length=int(digits);start=colon+1;end=start+length
        if end>len(raw):raise ValueError('truncated')
        pos=end;return raw[start:end]
    try:
        if not raw.startswith(b'd'):raise ValueError('dictionary required')
        parse()
        if pos!=len(raw) or not info_range:raise ValueError('missing info/trailing data')
    except (ValueError,IndexError,OverflowError):
        raise AppError('INVALID_TORRENT','种子文件结构无效，无法确认磁力身份')
    digest=hashlib.sha1(raw[info_range[0]:info_range[1]]).hexdigest()
    return 'magnet:?'+urllib.parse.urlencode({'xt':'urn:btih:'+digest,'dn':str(title)[:300]})
