import json
import posixpath
import secrets
import threading
import time
import urllib.parse

from .errors import AppError,ProviderError
from .http_client import HTTP
from .pan115 import normalize_cookie,cookie_uid

ALIST_115_DRIVER='115 Cloud'
ALIST_115_OFFLINE_TOOL='115 Cloud'
_ALIST_READY_STATUSES={'','init','initializing','loading','starting'}
_ALIST_COOKIE_REQUIRED=('UID','CID','SEID')
_ALIST_COOKIE_OPTIONAL=('KID',)


class Alist:
    def __init__(self,config,store,http=None):
        self.config,self.store,self.http=config,store,http or HTTP()
        self._token='';self._lock=threading.RLock()
        p=urllib.parse.urlsplit(config.alist_url)
        if p.scheme not in {'https','http'} or not p.netloc or p.username or p.password or p.query or p.fragment:
            raise AppError('INVALID_ALIST_URL','Alist 地址配置不正确',503)

    def credentials(self):
        token=self.config.alist_token
        password=self.config.alist_password
        stored=self.store.setting('alist_credentials')
        if stored:
            data=json.loads(self.store.decrypt(stored));token=data.get('token') or token;password=data.get('password') or password
        return token,password

    def _auth(self):
        with self._lock:
            if self._token:return self._token
            token,password=self.credentials()
            if token:self._token=token;return token
            if not password:raise AppError('ALIST_CREDENTIALS_REQUIRED','请在部署配置或连接设置中填写 Alist 管理凭据',409)
            data=self.http.json('POST',self.config.alist_url+'/api/auth/login',body={'username':self.config.alist_username,'password':password})
            token=(data.get('data') or {}).get('token')
            if data.get('code')!=200 or not token:raise ProviderError('ALIST_AUTH_FAILED','Alist 管理认证失败，请检查管理凭据')
            self._token=token;return token

    def reset_auth_cache(self):
        """Forget the in-memory admin token; credentials remain encrypted."""
        with self._lock:self._token=''

    def call(self,method,path,body=None,*,retry=True):
        data=self.http.json(method,self.config.alist_url+path,body=body,headers={'Authorization':self._auth()})
        if data.get('code')==401 and retry:
            with self._lock:self._token=''
            return self.call(method,path,body,retry=False)
        if data.get('code')!=200:
            if data.get('code')==404:raise ProviderError('ALIST_NOT_FOUND','Alist 尚未找到目标任务或文件，请刷新后再试')
            raise ProviderError('ALIST_REJECTED','Alist 未接受操作，请检查 115 存储与云下载权限')
        return data.get('data')

    def preflight(self):
        """Authenticate AList and verify the pinned 115 driver is available."""
        names=self.call('GET','/api/admin/driver/names')
        if not isinstance(names,list) or ALIST_115_DRIVER not in names:
            raise ProviderError('ALIST_115_DRIVER_UNAVAILABLE','当前 Alist 未提供 115 Cloud 驱动，请检查版本和管理员权限')
        return {'driver':ALIST_115_DRIVER,'drivers':names}

    def storages(self):
        out=[]
        for page in range(1,101):
            data=self.call('GET',f'/api/admin/storage/list?page={page}&per_page=100') or {}
            rows=data.get('content') or [];out.extend(rows)
            if len(out)>=int(data.get('total',len(out))):return out
            if not rows:raise ProviderError('ALIST_PAGINATION','Alist 存储列表未完整返回')
        raise ProviderError('ALIST_PAGINATION','Alist 存储列表过长')

    def get_storage(self,storage_id):
        data=self.call('GET','/api/admin/storage/get?id='+str(int(storage_id)))
        if not isinstance(data,dict):raise ProviderError('ALIST_INVALID_STORAGE','Alist 存储信息不完整')
        return data

    def inspect_reserved_mount(self,mount,marker,storage_id=None):
        """Classify an instance-owned mount without changing AList state."""
        matches=[s for s in self.storages() if s.get('mount_path')==mount]
        if not matches:return {'state':'missing','storage':None}
        if len(matches)>1:return {'state':'conflict','storage':None,'matches':matches}
        storage=matches[0]
        try:sid=int(storage.get('id'))
        except (TypeError,ValueError):sid=0
        try:expected_sid=int(storage_id) if storage_id is not None else None
        except (TypeError,ValueError):expected_sid=-1
        if (storage.get('remark')!=marker or (expected_sid is not None and sid!=expected_sid)
            or storage.get('driver')!=ALIST_115_DRIVER):
            return {'state':'tampered','storage':storage}
        return {'state':'managed','storage':storage}

    @staticmethod
    def _exact_mount(rows,mount,marker):
        matches=[s for s in rows if s.get('mount_path')==mount]
        if len(matches)>1:raise ProviderError('ALIST_MOUNT_CONFLICT','Alist 专用挂载点冲突')
        if matches and matches[0].get('remark')!=marker:
            raise ProviderError('ALIST_MOUNT_CONFLICT','该 Alist 挂载点不属于本网站，未进行覆盖')
        if matches and matches[0].get('driver')!=ALIST_115_DRIVER:
            raise ProviderError('ALIST_MOUNT_CONFLICT','该 Alist 挂载点使用了不受支持的驱动，未进行覆盖')
        return matches[0] if matches else None

    @staticmethod
    def _cookie_pairs(raw):
        normalized=normalize_cookie(raw)
        return dict(part.split('=',1) for part in normalized.split('; '))

    def _wait_for_ready(self,sid,cid,mount,marker,uid,cookie):
        """Allow a newly-created driver a short, bounded initialization window."""
        for attempt in range(4):
            storage=self.get_storage(sid)
            status=str(storage.get('status') or '').lower()
            if status=='work':
                return self.assert_scope(sid,cid,mount,marker,uid,cookie)
            if status not in _ALIST_READY_STATUSES or attempt==3:
                return self.assert_scope(sid,cid,mount,marker,uid,cookie)
            time.sleep(0.25*(attempt+1))
        return self.assert_scope(sid,cid,mount,marker,uid,cookie)

    def provision(self,cookie,uid,cid,mount,marker,storage_id=None):
        cookie=normalize_cookie(cookie)
        if cookie_uid(cookie)!=str(uid) or not str(cid).isdigit() or str(cid)=='0':
            raise AppError('STORAGE_SCOPE_MISMATCH','拒绝将日常根目录或其他账号挂载为转存目录',409)
        existing=self._exact_mount(self.storages(),mount,marker)
        matches=[existing] if existing else []
        if storage_id and matches and int(matches[0]['id'])!=int(storage_id):
            raise ProviderError('ALIST_MOUNT_CONFLICT','Alist 专用挂载记录已改变')
        # Protect the reserved path before exposing a newly-created storage.
        self.provision_privacy(mount,marker)
        payload={'mount_path':mount,'driver':ALIST_115_DRIVER,'cache_expiration':1,
                 'addition':json.dumps({'cookie':cookie,'root_folder_id':str(cid),'page_size':1000,'limit_rate':2,'qrcode_token':''}),
                 'remark':marker,'disabled':False,'disable_index':True,'enable_sign':True,
                 'web_proxy':True,'webdav_policy':'native_proxy','order_by':'name','order_direction':'asc'}
        if matches:
            payload['id']=matches[0]['id'];self.call('POST','/api/admin/storage/update',payload);sid=int(payload['id'])
        else:
            result=self.call('POST','/api/admin/storage/create',payload) or {};sid=int(result.get('id') or 0)
            if sid<=0:
                # Some AList builds return an empty body after a successful
                # create. Re-read only the exact owned marker.
                created=self._exact_mount(self.storages(),mount,marker)
                if created:
                    try:sid=int(created.get('id') or 0)
                    except (TypeError,ValueError):sid=0
        if sid<=0:raise ProviderError('ALIST_STORAGE_CREATE_FAILED','Alist 未返回有效存储记录')
        self._wait_for_ready(sid,cid,mount,marker,uid,cookie)
        return sid

    def provision_privacy(self,mount,marker):
        rows=self.metas()
        matches=[m for m in rows if m.get('path')==mount]
        if len(matches)>1 or (matches and matches[0].get('readme')!=marker):
            raise ProviderError('ALIST_META_CONFLICT','Alist 专用目录权限记录冲突，未覆盖其他记录')
        secret=self.store.setting('alist_mount_password')
        if not secret:
            secret=self.store.encrypt(secrets.token_urlsafe(32));self.store.set_setting('alist_mount_password',secret)
        payload={'path':mount,'password':self.store.decrypt(secret),'p_sub':True,'hide':'.*','h_sub':True,'readme':marker,'write':False,'w_sub':True}
        if matches:
            payload['id']=matches[0]['id'];self.call('POST','/api/admin/meta/update',payload);mid=payload['id']
        else:
            self.call('POST','/api/admin/meta/create',payload)
            matches=[m for m in self.metas() if m.get('path')==mount and m.get('readme')==marker]
            if len(matches)!=1:raise ProviderError('ALIST_META_NOT_READY','未能确认专用目录的访问保护')
            mid=matches[0]['id']
        self.store.set_setting('alist_meta_id',str(mid))

    def metas(self):
        rows=[]
        for page in range(1,101):
            result=self.call('GET',f'/api/admin/meta/list?page={page}&per_page=100') or {}
            chunk=result.get('content') or [];rows.extend(chunk)
            if len(rows)>=int(result.get('total',len(rows))):return rows
            if not chunk:raise ProviderError('ALIST_META_INCOMPLETE','Alist 目录权限列表未完整返回')
        raise ProviderError('ALIST_META_INCOMPLETE','Alist 目录权限列表过长')

    def assert_scope(self,sid,cid,mount,marker,uid,expected_cookie=None):
        try:s=self.get_storage(sid)
        except ProviderError as error:
            if error.code=='ALIST_NOT_FOUND':
                raise ProviderError('ALIST_STORAGE_MISSING','Alist 专用存储已不存在，未进行转存和下载')
            raise
        try:a=json.loads(s.get('addition') or '{}')
        except ValueError:raise ProviderError('ALIST_STORAGE_CHANGED','Alist 存储配置不正确')
        if (s.get('driver')!=ALIST_115_DRIVER or s.get('mount_path')!=mount or s.get('remark')!=marker
            or str(a.get('root_folder_id'))!=str(cid) or s.get('disabled')
            or not s.get('web_proxy') or a.get('qrcode_token') or s.get('status')!='work'
            or not a.get('cookie') or not a.get('page_size') or not a.get('limit_rate')):
            raise ProviderError('ALIST_STORAGE_CHANGED','Alist 专用存储已改变，已停止转存和下载')
        try:actual_uid=cookie_uid(normalize_cookie(a.get('cookie','')))
        except (AppError,AttributeError):raise ProviderError('ALIST_STORAGE_CHANGED','Alist CK 配置已改变')
        if actual_uid!=str(uid):raise ProviderError('ALIST_ACCOUNT_MISMATCH','Alist 存储账号与 115 连接不一致')
        if expected_cookie:
            original,current=self._cookie_pairs(expected_cookie),self._cookie_pairs(a['cookie'])
            if any(original.get(k)!=current.get(k) for k in _ALIST_COOKIE_REQUIRED):
                raise ProviderError('ALIST_COOKIE_MISMATCH','Alist 的 CK 与网站绑定记录不一致，请重新绑定同一份 CK')
            if any(k in original and original.get(k)!=current.get(k) for k in _ALIST_COOKIE_OPTIONAL):
                raise ProviderError('ALIST_COOKIE_MISMATCH','Alist 的 CK 与网站绑定记录不一致，请重新绑定同一份 CK')
        mid=self.store.setting('alist_meta_id')
        if not mid:raise ProviderError('ALIST_META_NOT_READY','尚未建立专用目录访问保护')
        meta=self.call('GET','/api/admin/meta/get?id='+str(int(mid))) or {}
        secret=self.store.setting('alist_mount_password')
        if (meta.get('path')!=mount or meta.get('readme')!=marker or not meta.get('p_sub')
            or not secret or meta.get('password')!=self.store.decrypt(secret)):
            raise ProviderError('ALIST_META_CHANGED','Alist 专用目录访问保护已改变，已停止操作')
        return s

    @staticmethod
    def safe_path(root,relative=''):
        if not root.startswith('/mikan115-') or '..' in relative.split('/') or '\\' in relative or '\0' in relative:
            raise AppError('UNSAFE_PATH','目标文件路径不正确',403)
        q=posixpath.normpath(root+'/'+relative.lstrip('/'))
        if q!=root and not q.startswith(root+'/'):raise AppError('UNSAFE_PATH','目标文件超出专用转存目录',403)
        return q

    def list_files(self,path):
        data=self.call('POST','/api/fs/list',{'path':path,'password':'','page':1,'per_page':0,'refresh':True}) or {}
        rows=data.get('content') or []
        if not isinstance(rows,list):raise ProviderError('ALIST_INVALID_FILES','Alist 文件列表格式异常')
        return rows

    def submit_cloud(self,task_path,magnet):
        data=self.call('POST','/api/fs/add_offline_download',{'path':task_path,'urls':[magnet],'tool':ALIST_115_OFFLINE_TOOL,'delete_policy':'delete_never'}) or {}
        tasks=data.get('tasks') or []
        if len(tasks)!=1 or not tasks[0].get('id'):
            raise ProviderError('SUBMIT_RESULT_UNKNOWN','云下载提交结果待核实，请勿重复提交',uncertain=True)
        return str(tasks[0]['id'])

    def task_info(self,tid):
        return self.call('POST','/api/admin/task/offline_download/info?tid='+urllib.parse.quote(tid,safe=''),{})

    def proxy_stream(self,path,range_header=None):
        data=self.call('POST','/api/fs/get',{'path':path,'password':''}) or {}
        if data.get('is_dir'):raise AppError('NOT_A_FILE','不能直接下载目录',400)
        sign=str(data.get('sign') or '')
        query='?'+urllib.parse.urlencode({'sign':sign}) if sign else ''
        url=self.config.alist_url+'/p/'+urllib.parse.quote(path.lstrip('/'),safe='/')+query
        headers={'Authorization':self._auth(),'User-Agent':'Mozilla/5.0 Mikan115/0.1'}
        if range_header:headers['Range']=range_header
        return self.http.open('GET',url,headers=headers)
