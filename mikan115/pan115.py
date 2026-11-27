import re
import urllib.parse

from .errors import AppError, ProviderError
from .http_client import HTTP


def normalize_cookie(raw):
    if not isinstance(raw,str) or len(raw)>16384 or any(ord(x)<32 or ord(x)==127 for x in raw):
        raise AppError('INVALID_COOKIE','CK 格式不正确，不能包含换行或控制字符')
    pairs={}
    for part in raw.strip().split(';'):
        part=part.strip()
        if not part:continue
        if '=' not in part:raise AppError('INVALID_COOKIE','CK 格式不正确')
        key,value=part.split('=',1);key=key.strip();value=value.strip()
        if key in pairs:raise AppError('INVALID_COOKIE','CK 中存在重复字段')
        if not re.fullmatch(r'[A-Za-z0-9_\-]+',key) or not value:raise AppError('INVALID_COOKIE','CK 格式不正确')
        pairs[key]=value
    if not all(pairs.get(x) for x in ('UID','CID','SEID')):
        raise AppError('INVALID_COOKIE','CK 至少需要 UID、CID 和 SEID，已有 KID 时也请保留')
    if not re.fullmatch(r'\d+(?:_[A-Za-z0-9_\-]+)*',pairs['UID']):
        raise AppError('INVALID_COOKIE','CK 的 UID 格式不正确')
    # Preserve device/session identity, including third-party session fields.
    return '; '.join(f'{k}={v}' for k,v in pairs.items())


def cookie_uid(cookie):
    return re.search(r'(?:^|;\s*)UID=(\d+)',cookie).group(1)


class Pan115:
    def __init__(self,cookie,http=None,*,my_base='https://my.115.com',web_base='https://webapi.115.com',offline_base='https://lixian.115.com'):
        self.cookie=normalize_cookie(cookie);self.http=http or HTTP()
        self.my_base,self.web_base,self.offline_base=my_base,web_base,offline_base

    def _call(self,method,url,**kwargs):
        data=self.http.json(method,url,headers={'Cookie':self.cookie},**kwargs)
        if data.get('state') not in (True,1):
            errno=str(data.get('errno') or data.get('errcode') or '')
            if errno in {'99','911','401','40101017','990001','990002','70005'}:
                raise ProviderError('COOKIE_EXPIRED','115 会话失效或需要在原客户端完成验证，请管理员手动更新 CK')
            raise ProviderError('PAN115_REJECTED','115 未接受请求，请检查账号权限、配额和会话状态')
        return data

    def identity(self):
        data=self._call('GET',self.my_base+'/?ct=ajax&ac=nav').get('data') or {}
        uid=str(data.get('user_id') or '')
        if not uid.isdigit() or uid!=cookie_uid(self.cookie):
            raise ProviderError('COOKIE_IDENTITY_MISMATCH','115 返回的账号与 CK 不一致')
        return {'uid':uid,'name':str(data.get('user_name') or uid)}

    def list_files(self,cid):
        if not str(cid).isdigit():raise AppError('INVALID_FOLDER','无效的 115 目录标识')
        result=[];offset=0
        for _ in range(100):
            query=urllib.parse.urlencode({'aid':1,'cid':str(cid),'offset':offset,'limit':1000,'show_dir':1,'format':'json','fc_mix':1})
            data=self._call('GET',self.web_base+'/files?'+query)
            actual=str(data.get('cid') or '')
            breadcrumb=data.get('path') or []
            if not actual and breadcrumb:actual=str(breadcrumb[-1].get('cid',''))
            if actual!=str(cid):raise ProviderError('FOLDER_SCOPE_MISMATCH','115 目录校验失败，已停止操作')
            items=data.get('data') or []
            if not isinstance(items,list):raise ProviderError('PAN115_INVALID_FILES','115 文件列表格式异常')
            result.extend(items);offset+=len(items)
            total=int(data.get('count',offset))
            if offset>=total:return result
            if not items:raise ProviderError('PAN115_PAGINATION','115 目录分页不完整，已停止操作')
        raise ProviderError('PAN115_PAGINATION','115 目录条目过多，无法确认完整列表')

    def create_folder(self,parent_cid,name):
        if not re.fullmatch(r'[A-Za-z0-9_\-]{3,100}',name):raise AppError('INVALID_FOLDER_NAME','目录名称不正确')
        data=self._call('POST',self.web_base+'/files/add',form={'pid':str(parent_cid),'cname':name})
        cid=str(data.get('cid') or (data.get('data') or {}).get('cid') or '')
        if not cid.isdigit() or cid=='0' or cid==str(parent_cid):
            raise ProviderError('INVALID_CREATED_FOLDER','115 没有返回有效的独立目录')
        return cid

    def verify_child(self,parent_cid,child_cid,name):
        matches=[x for x in self.list_files(parent_cid) if not x.get('fid') and str(x.get('cid'))==str(child_cid) and str(x.get('n') or x.get('name') or '')==name]
        if len(matches)!=1:raise ProviderError('FOLDER_SCOPE_MISMATCH','专用转存目录已改变，请管理员重新检查连接')

    def ensure_task_folder(self,parent_cid,infohash):
        if not re.fullmatch('[a-f0-9]{40}',infohash):raise AppError('INVALID_INFOHASH','磁力标识不正确')
        matches=[x for x in self.list_files(parent_cid) if not x.get('fid') and str(x.get('n') or x.get('name') or '')==infohash]
        if len(matches)>1:raise ProviderError('AMBIGUOUS_TASK_FOLDER','发现重复任务目录，已停止自动转存')
        cid=str(matches[0].get('cid')) if matches else self.create_folder(parent_cid,infohash)
        self.verify_child(parent_cid,cid,infohash)
        return cid

    def find_offline_task(self,infohash,task_cid):
        for page in range(1,101):
            query=urllib.parse.urlencode({'ct':'lixian','ac':'task_lists','page':page})
            data=self._call('POST',self.offline_base+'/lixian/?'+query,form={})
            for task in data.get('tasks') or []:
                if str(task.get('info_hash') or '').lower()==infohash:
                    if str(task.get('wp_path_id'))!=str(task_cid):
                        raise ProviderError('REMOTE_TASK_FOLDER_CONFLICT','相同磁力任务已存在于其他目录，未改动原任务')
                    return task
            if page>=int(data.get('page_count') or 1):return None
        raise ProviderError('PAN115_PAGINATION','115 离线任务分页未能完整读取')

    def file_parent(self,task_cid,relative):
        decoded=urllib.parse.unquote(relative)
        parts=relative.split('/')
        if not relative or relative.startswith('/') or len(parts)>9 or any(p in {'','.','..'} for p in decoded.split('/')) or any(c in decoded for c in ('\\','\0','\r','\n')):raise AppError('UNSAFE_PATH','文件路径无效',403)
        cid=str(task_cid)
        if not cid.isdigit() or cid=='0':raise AppError('UNSAFE_PATH','不能改动日常目录',403)
        for name in parts[:-1]:
            rows=[r for r in self.list_files(cid) if not r.get('fid') and str(r.get('n') or r.get('name'))==name]
            if len(rows)!=1:raise AppError('RENAME_SCOPE_CHANGED','文件子目录未确认',409)
            cid=str(rows[0].get('cid') or '')
            if not cid.isdigit() or cid=='0':raise AppError('UNSAFE_PATH','目录标识无效',403)
        return cid,self.list_files(cid)

    def locate_file(self,task_cid,relative):
        parent,rows=self.file_parent(task_cid,relative);name=relative.rsplit('/',1)[-1]
        matches=[r for r in rows if r.get('fid') and str(r.get('n') or r.get('name'))==name]
        if len(matches)!=1:raise AppError('RENAME_SOURCE_CHANGED','原文件未确认',409)
        return parent,matches[0]

    def rename_file(self,fid,name):
        if not str(fid).isdigit() or not name or name in {'.','..'} or len(name)>240 or any(c in name for c in ('/','\\','\0','\r','\n')):raise AppError('INVALID_FILE_NAME','文件名无效')
        # Same endpoint/form used by the pinned 115 driver; callers prove scope first.
        self._call('POST',self.web_base+'/files/batch_rename',form={'fid':str(fid),'file_name':name,'files_new_name['+str(fid)+']':name})
