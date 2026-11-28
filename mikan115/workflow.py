import base64
import hashlib
import json
import posixpath
import re
import secrets
import threading
import time
import urllib.parse

from .errors import AppError,ProviderError
from .pan115 import Pan115,normalize_cookie
from .cloud_layout import root_name


ACTIVE={'NEW','SUBMITTING','SUBMIT_UNKNOWN','CLOUD_DOWNLOADING','WAITING_FILES','AUTH_REQUIRED'}
POLLABLE={'SUBMIT_UNKNOWN','CLOUD_DOWNLOADING','WAITING_FILES'}
# github.com/xhofe/tache v0.1.5: validated against the pinned AList source.
# Filled from the dependency's state contract (also checked by protocol tests).
ALIST_SUCCEEDED=2
ALIST_FAILED={4,7}


def normalize_magnet(value):
    if not isinstance(value,str) or len(value)>16000 or any(ord(x)<32 for x in value):
        raise AppError('INVALID_MAGNET','磁力链接格式不正确')
    p=urllib.parse.urlsplit(value.strip())
    if p.scheme.lower()!='magnet' or p.netloc or p.path or p.fragment:
        raise AppError('INVALID_MAGNET','请使用包含 BTIH 的 magnet 磁力链接')
    args=urllib.parse.parse_qs(p.query,keep_blank_values=True)
    ids=[x[9:] for x in args.get('xt',[]) if x.lower().startswith('urn:btih:')]
    if len(ids)!=1:raise AppError('INVALID_MAGNET','磁力链接必须包含唯一的 BTIH 标识')
    ident=ids[0]
    if re.fullmatch('[a-fA-F0-9]{40}',ident):ident=ident.lower()
    elif re.fullmatch('[a-zA-Z2-7]{32}',ident):ident=base64.b32decode(ident.upper()).hex()
    else:raise AppError('INVALID_MAGNET','磁力链接的 BTIH 标识无效')
    query=[('xt','urn:btih:'+ident)]
    if args.get('dn'):query.append(('dn',args['dn'][0][:300]))
    for tracker in args.get('tr',[])[:16]:
        t=urllib.parse.urlsplit(tracker)
        if t.scheme in {'https','http','udp'} and t.netloc and len(tracker)<1024:query.append(('tr',tracker))
    return ident,'magnet:?'+urllib.parse.urlencode(query)


class Workflow:
    def __init__(self,store,config,alist,pan_factory=Pan115):
        self.store,self.config,self.alist,self.pan_factory=store,config,alist,pan_factory
        self.connection_lock=threading.RLock()
        self.worker_id=secrets.token_hex(12)
        self.next_connection_check=0.
        self.layout=None

    def marker(self,uid):return f'mikan115:{self.store.instance_id}:{uid}'

    def connection_status(self):
        b=self.store.binding(False)
        pending=bool(self.store.setting('pending_115_cookie'))
        identity=json.loads(self.store.decrypt(self.store.setting('verified_115_identity'))) if self.store.setting('verified_115_identity') else {}
        alist=self.alist.status()
        return {'configured':bool(b or pending),'ready':bool(b and b['ready']),
                'account_uid':identity.get('uid') or (b['account_uid'] if b else None),
                'account_name':identity.get('name') or (b['account_name'] if b else None),
                'transfer_folder':b['root_name'] if b else None,
                'directory_name':self.store.setting('cloud_root_name','anime'),
                'cookie_verified':self.cookie_verified(),
                'cookie_error':self.store.setting('cookie_error') or None,
                'alist_mount':b['mount_path'] if b else None,
                'cookie_source':self.store.setting('cookie_source','third_party'),
                'cookie_configured':bool(b or pending),'auto_relogin':False,'qr_login_enabled':False,
                'alist_configured':alist['configured'],'alist_managed':alist['managed'],
                'connection_error':self.store.setting('connection_error') or None,
                'connection_error_code':self.store.setting('connection_error_code') or None,
                'alist_url':alist['url']}

    def cookie_verified(self):
        b=self.store.binding(False);saved=self.store.setting('pending_115_cookie') or (b and b['cookie_encrypted'])
        if not saved or self.store.setting('cookie_error') or self.store.setting('connection_error_code')=='COOKIE_EXPIRED':return False
        return hashlib.sha256(self.store.decrypt(saved).encode()).hexdigest()==self.store.setting('verified_115_fingerprint')

    def _verify_cookie(self,cookie,cookie_source):
        prior=self.store.binding(False)
        saved=self.store.setting('pending_115_cookie') or (prior and prior['cookie_encrypted'])
        if not cookie and not saved:raise AppError('COOKIE_REQUIRED','请输入 115 CK')
        cookie=normalize_cookie(cookie or self.store.decrypt(saved));pan=self.pan_factory(cookie);identity=pan.identity()
        if prior and prior['account_uid']!=identity['uid']:
            if self.store.one("SELECT 1 FROM transfers WHERE state NOT IN ('FILES_READY','FAILED','CANCELED') LIMIT 1"):
                raise AppError('ACCOUNT_HAS_ACTIVE_TASKS','仍有未结束的任务，不能切换 115 账号',409)
            raise AppError('ACCOUNT_SWITCH_REQUIRES_NEW_INSTANCE','请为其他 115 账号使用新的数据目录',409)
        with self.store.transaction() as db:
            for key,value in [('pending_115_cookie',self.store.encrypt(cookie)),('cookie_source',cookie_source),
                              ('verified_115_identity',self.store.encrypt(json.dumps(identity))),
                              ('verified_115_fingerprint',hashlib.sha256(cookie.encode()).hexdigest())]:
                db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
            expired=db.execute("SELECT value FROM settings WHERE key='connection_error_code'").fetchone()
            db.execute("DELETE FROM settings WHERE key='cookie_error'")
            if expired and expired['value']=='COOKIE_EXPIRED':db.execute("DELETE FROM settings WHERE key IN ('connection_error','connection_error_code')")
        return prior,pan,identity,cookie

    def verify_cookie(self,cookie='',*,cookie_source='third_party'):
        if not isinstance(cookie,str) or cookie_source not in {'third_party','existing_session'}:raise AppError('INVALID_CONNECTION_FIELDS','CK 格式不正确')
        if self.store.setting('update_maintenance')=='1':raise AppError('UPDATE_IN_PROGRESS','更新中，请稍后再试',503)
        with self.connection_lock:
            try:self._verify_cookie(cookie,cookie_source)
            except AppError as error:
                self.store.set_setting('cookie_error',error.message);raise
            self.store.set_setting('connection_directory_pending','1')
            with self.store.transaction() as db:db.execute('UPDATE binding SET ready=0 WHERE id=1')
            return self.connection_status()

    def configure_directory(self,name):
        name=root_name(name)
        if self.store.setting('update_maintenance')=='1':raise AppError('UPDATE_IN_PROGRESS','更新中，请稍后再试',503)
        with self.connection_lock:
            if not self.cookie_verified():raise AppError('COOKIE_VERIFICATION_REQUIRED','请先验证 CK',409)
            try:return self._bind('',self.store.setting('cookie_source','third_party'),'','',directory=name)
            except AppError as error:
                with self.store.transaction() as db:db.execute('UPDATE binding SET ready=0 WHERE id=1')
                self.store.set_setting('connection_error',error.message);self.store.set_setting('connection_error_code',error.code);raise

    def configure_alist(self,body):
        with self.connection_lock:
            if self.store.one("SELECT 1 FROM transfers WHERE state IN ('SUBMITTING','SUBMIT_UNKNOWN','CLOUD_DOWNLOADING','WAITING_FILES') LIMIT 1"):
                if body.get('check') is not True:
                    raise AppError('ACTIVE_TRANSFERS','转存处理中，请完成后修改 Alist 连接',409)
            if body.get('check') is True:
                if set(body)!={'check'}:raise AppError('INVALID_ALIST_SETTINGS','Alist 设置无效')
                return self.alist.status(check=True)
            result=self.alist.configure(body)
            if self.store.binding(False):
                with self.store.transaction() as db:db.execute('UPDATE binding SET ready=0 WHERE id=1')
            if self.store.binding(False) or self.store.setting('pending_115_cookie'):
                try:self.bind()
                except AppError:return {**result,'connection':self.connection_status()}
            return {**result,'connection':self.connection_status()}

    def bind(self,cookie='',*,cookie_source=None,alist_password='',alist_token=''):
        cookie_source=cookie_source or self.store.setting('cookie_source','third_party')
        if cookie_source not in {'third_party','existing_session'}:raise AppError('INVALID_COOKIE_SOURCE','请选择已有的第三方或其他已有会话 CK')
        if not all(isinstance(value,str) for value in (cookie,alist_password,alist_token)):
            raise AppError('INVALID_CONNECTION_FIELDS','连接设置格式不正确')
        with self.connection_lock:
            try:
                return self._bind(cookie,cookie_source,alist_password,alist_token)
            except AppError as error:
                with self.store.transaction() as db:db.execute('UPDATE binding SET ready=0 WHERE id=1')
                self.store.set_setting('connection_error',error.message)
                self.store.set_setting('connection_error_code',error.code)
                raise

    def _bind(self,cookie,cookie_source,alist_password,alist_token,*,directory=None):
        prior,pan,identity,cookie=self._verify_cookie(cookie,cookie_source)
        uid=identity['uid']
        encrypted=self.store.encrypt(cookie)
        if alist_password or alist_token:
            self.store.set_setting('alist_credentials',self.store.encrypt(json.dumps({'password':alist_password,'token':alist_token})))
            self.alist.reset_auth_cache()
        self.alist.preflight()
        name=root_name(directory if directory is not None else self.store.setting('cloud_root_name','anime'))
        mount='/mikan115-'+self.store.instance_id[:8]
        if prior:
            cid=prior['root_cid']
            if self.layout:
                prior=self.layout.root(pan,prior,target=name,allow_current=directory is not None);name=prior['root_name']
            else:pan.verify_child('0',cid,name)
        else:
            from .cloud_layout import normalized
            matches=[f for f in pan.list_files('0') if normalized(str(f.get('n') or f.get('name') or ''))==normalized(name)]
            if matches:raise AppError('DEDICATED_FOLDER_ALREADY_EXISTS','目录名称已被使用，请换一个名称',409)
            cid=pan.create_folder('0',name)
            self.store.set_setting('cloud_root_name',name)
        with self.store.transaction() as c:
            c.execute('''INSERT INTO binding VALUES(1,?,?,?,?,?,?,?,0,?)
                ON CONFLICT(id) DO UPDATE SET account_name=excluded.account_name,
                cookie_encrypted=excluded.cookie_encrypted,ready=0,updated_at=excluded.updated_at''',
                (uid,identity['name'],encrypted,cid,name,mount,prior['storage_id'] if prior else 0,time.time()))
        pan.verify_child('0',cid,name)
        sid=self.alist.provision(cookie,uid,cid,mount,self.marker(uid),prior['storage_id'] if prior and prior['storage_id'] else None)
        self.alist.list_files(mount)
        with self.store.transaction() as c:
            c.execute('UPDATE binding SET storage_id=?,ready=1,updated_at=? WHERE id=1',(sid,time.time()))
            c.execute("DELETE FROM settings WHERE key IN ('pending_115_cookie','connection_error','connection_error_code','connection_directory_pending','cookie_error')")
            c.execute("INSERT INTO settings VALUES('cloud_directory_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        return self.connection_status()

    def reconcile_connection(self):
        if not self.alist.status()['managed'] or not any(self.alist.credentials()):return
        now=time.monotonic()
        if now<self.next_connection_check:return
        self.next_connection_check=now+60
        with self.connection_lock:
            if self.store.setting('connection_directory_pending')=='1':return
            pending=self.store.setting('pending_115_cookie')
            b=self.store.binding(False)
            if not b and not pending:return
            if self.store.setting('connection_error_code') in {'COOKIE_EXPIRED','DEDICATED_FOLDER_ALREADY_EXISTS','ALIST_MOUNT_CONFLICT','ALIST_META_CONFLICT','ALIST_STORAGE_CHANGED'}:return
            try:
                if pending or not b['ready']:
                    self.bind()
                else:
                    marker=self.marker(b['account_uid'])
                    status=self.alist.inspect_reserved_mount(b['mount_path'],marker,b['storage_id'])
                    if status['state']=='missing':self.bind()
                    elif status['state']!='managed':
                        with self.store.transaction() as c:c.execute('UPDATE binding SET ready=0 WHERE id=1')
                        raise AppError('ALIST_MOUNT_CONFLICT','Alist 专用存储已改变，请核对后修复连接',409)
            except AppError as error:
                self.store.set_setting('connection_error',error.message)
                self.store.set_setting('connection_error_code',error.code)

    def scope(self,task=None):
        b=self.store.binding()
        if task and task['account_uid']!=b['account_uid']:
            raise AppError('TASK_ACCOUNT_MISMATCH','任务所属账号与当前连接不一致',409)
        cookie=self.store.decrypt(b['cookie_encrypted'])
        pan=self.pan_factory(cookie)
        if self.layout and b['root_name']!=self.store.setting('cloud_root_name','anime'):b=self.layout.root(pan,b)
        pan.verify_child('0',b['root_cid'],b['root_name'])
        self.alist.assert_scope(b['storage_id'],b['root_cid'],b['mount_path'],self.marker(b['account_uid']),b['account_uid'],cookie)
        if task and task.get('task_cid'):
            layout=self.layout.row(task['id']) if self.layout else None
            if layout and layout['status']=='PUBLISHED':self.layout.scope(task,pan,b,layout)
            else:
                pan.verify_child(b['root_cid'],task['task_cid'],task['infohash'])
                expected=self.alist.safe_path(b['mount_path'],task['infohash'])
                if task['task_path']!=expected:raise AppError('TASK_SCOPE_MISMATCH','任务目录不匹配，已停止操作',409)
        return b,pan

    def create(self,user,magnet,title='',work_id='',*,subscription_id=None,subscription_revision=None,enqueue_only=False,naming=None):
        infohash,magnet=normalize_magnet(magnet)
        acquisition=(naming or {}).get('acquisition') or {}
        if naming and not naming.get('scope_key') and self.layout and work_id:
            from .inventory import work_scope
            naming={**naming,'scope_key':work_scope(self.layout.catalog,self.layout.catalog.work(work_id))}
        if naming and naming.get('scope_key') and not acquisition:
            from . import releases
            rule=releases.rules(naming.get('rules') or {})
            acquisition={'scope_key':naming['scope_key'],'episodes':releases.normalized_episodes(naming.get('release') or releases.parse(title),rule),
                         'destination':'cloud','dedupe':rule['dedupe'],'version':(naming.get('release') or {}).get('version','unknown')}
        eps=sorted({e for e in acquisition.get('episodes',[]) if type(e) is int and 0<e<=9999})
        variant=('hash:'+infohash if acquisition.get('upgrade') or acquisition.get('dedupe')=='hash' else
                 'version:'+str(acquisition.get('version','unknown')) if acquisition.get('dedupe')=='episode_version' else '')
        destination=acquisition.get('destination','cloud')
        if destination not in {'cloud','vps'}:raise AppError('INVALID_DESTINATION','请选择保存位置')
        with self.connection_lock:
            b=self.store.binding();now=time.time()
            existing=self.store.one('SELECT t.*,n.metadata FROM transfers t LEFT JOIN transfer_naming n ON n.transfer_id=t.id WHERE t.account_uid=? AND t.infohash=?',(b['account_uid'],infohash))
            if existing and naming and naming.get('scope_key'):
                from .inventory import work_scope
                old_scope=json.loads(existing.get('metadata') or '{}').get('scope_key')
                if not old_scope and existing['work_id'] and self.layout:
                    old_scope=work_scope(self.layout.catalog,self.layout.catalog.work(existing['work_id']))
                if old_scope and old_scope!=naming['scope_key']:raise AppError('RESOURCE_WORK_CONFLICT','相同资源已关联其他作品，请核对作品与季度',409)
            with self.store.transaction() as c:
                maintenance=c.execute("SELECT value FROM settings WHERE key='update_maintenance'").fetchone()
                if maintenance and maintenance['value']=='1':raise AppError('UPDATE_IN_PROGRESS','更新中，请稍后再试',503)
                if subscription_id:
                    sub=c.execute('SELECT enabled,user_id,revision,removed FROM subscriptions WHERE id=?',(subscription_id,)).fetchone()
                    paused=c.execute("SELECT value FROM settings WHERE key='rss_paused'").fetchone()
                    if not sub or not sub['enabled'] or sub['removed'] or sub['user_id']!=user['id'] or (subscription_revision is not None and sub['revision']!=subscription_revision) or (paused and paused['value']=='1'):return None
                reuse=None
                if eps and acquisition.get('scope_key'):
                    candidates=[]
                    for episode in eps:
                        claims=c.execute('''SELECT a.transfer_id,t.state,n.metadata FROM acquisition_claims a
                            JOIN transfers t ON t.id=a.transfer_id LEFT JOIN transfer_naming n ON n.transfer_id=t.id
                            WHERE a.user_id=? AND a.scope_key=? AND a.episode=? AND a.variant=?
                            ORDER BY a.destination=? DESC''',(user['id'],acquisition['scope_key'],episode,variant,destination)).fetchall()
                        valid=[]
                        for claim in claims:
                            if claim['state'] in {'FAILED','CANCELED'}:continue
                            if claim['state']=='FILES_READY' and 'verified_tasks' in acquisition and claim['transfer_id'] not in acquisition['verified_tasks']:continue
                            meta=json.loads(claim['metadata'] or '{}')
                            from . import releases
                            if meta.get('release') and releases.admit({'title':title,'release':meta['release']},{**releases.rules((naming or {}).get('rules') or {}),'allow_batches':True}):continue
                            valid.append(claim['transfer_id'])
                        candidates.append(set(valid))
                    common=set.intersection(*candidates) if candidates and all(candidates) else set()
                    if common:reuse=sorted(common)[0]
                    elif any(candidates):raise AppError('EPISODES_ALREADY_QUEUED','部分剧集已在处理中，请刷新清单',409)
                row=c.execute('SELECT id FROM transfers WHERE id=?',(reuse,)).fetchone() if reuse else c.execute('SELECT id FROM transfers WHERE account_uid=? AND infohash=?',(b['account_uid'],infohash)).fetchone()
                tid=row['id'] if row else secrets.token_hex(16)
                if not row:c.execute('INSERT INTO transfers(id,account_uid,infohash,magnet,title,work_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,\'NEW\',?,?)',
                    (tid,b['account_uid'],infohash,magnet,str(title or infohash)[:300],str(work_id)[:150],now,now))
                elif naming and not c.execute('SELECT work_id FROM transfers WHERE id=?',(tid,)).fetchone()['work_id']:
                    c.execute('UPDATE transfers SET work_id=? WHERE id=?',(str(work_id),tid))
                c.execute('INSERT OR IGNORE INTO transfer_owners VALUES(?,?)',(tid,user['id']))
                if eps and acquisition.get('scope_key'):
                    for episode in eps:
                        c.execute('''INSERT INTO acquisition_claims VALUES(?,?,?,?,?,?)
                            ON CONFLICT(user_id,scope_key,episode,variant,destination) DO UPDATE SET transfer_id=excluded.transfer_id''',
                            (user['id'],acquisition['scope_key'],episode,variant,destination,tid))
                if naming and (not row or c.execute('SELECT work_id FROM transfers WHERE id=?',(tid,)).fetchone()['work_id']==str(work_id)):
                    c.execute('INSERT OR IGNORE INTO transfer_naming VALUES(?,?,?,?,?,?)',
                              (tid,json.dumps(naming,ensure_ascii=False),int(naming['rules'].get('rename',True)),'PENDING',None,now))
            if not row:
                self.store.event(tid,'CREATED','直接创建转存任务，无审批环节')
                if not enqueue_only:self.submit(tid)
            return self.get(user,tid)

    def claim(self,tid,states):
        with self.store.transaction() as c:
            row=c.execute('SELECT * FROM transfers WHERE id=?',(tid,)).fetchone()
            if not row or row['state'] not in states or row['lease_until']>time.time():return None
            c.execute('UPDATE transfers SET lease_until=?,lease_owner=? WHERE id=?',(time.time()+180,self.worker_id,tid))
            return dict(row)

    def update(self,tid,**values):
        allowed={'state','task_cid','task_path','alist_task_id','cloud_file_id','progress','error_code','error_message','lease_until','lease_owner','attempts','next_poll_at'}
        if not set(values)<=allowed:raise RuntimeError('Unknown transfer column')
        values['updated_at']=time.time()
        with self.store.transaction() as c:c.execute('UPDATE transfers SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),tid))

    def submit(self,tid):
        with self.connection_lock:
            task=self.claim(tid,{'NEW'})
            if not task:return
            self.update(tid,state='SUBMITTING',error_code=None,error_message=None)
            cloud_post_started=False
            try:
                b,pan=self.scope(task)
                cid=pan.ensure_task_folder(b['root_cid'],task['infohash'])
                path=self.alist.safe_path(b['mount_path'],task['infohash'])
                self.update(tid,task_cid=cid,task_path=path)
                task.update(task_cid=cid,task_path=path)
                # Do not resubmit a hash which already exists in a different daily folder.
                existing=pan.find_offline_task(task['infohash'],cid)
                if existing:
                    self.update(tid,state='CLOUD_DOWNLOADING',progress=float(existing.get('percentDone') or 0),next_poll_at=0)
                    self.store.event(tid,'RECOVERED','已关联专用目录内的同一磁力任务')
                else:
                    self.alist.list_files(b['mount_path']) # refresh a newly created child folder
                    cloud_post_started=True
                    upstream_id=self.alist.submit_cloud(path,task['magnet'])
                    self.update(tid,state='CLOUD_DOWNLOADING',alist_task_id=upstream_id,next_poll_at=0)
                    self.store.event(tid,'CLOUD_SUBMITTED','已发送至 115 专用目录，等待云下载完成')
            except AppError as e:
                state='SUBMIT_UNKNOWN' if e.uncertain and cloud_post_started else ('AUTH_REQUIRED' if e.code=='COOKIE_EXPIRED' else 'FAILED')
                self.update(tid,state=state,error_code=e.code,error_message=e.message,next_poll_at=time.time()+15)
                self.store.event(tid,state,e.message)
            finally:self.update(tid,lease_until=0,lease_owner=None)

    def authorize(self,user,tid):
        task=self.store.one('SELECT * FROM transfers WHERE id=?',(tid,))
        allowed=task and (user['is_admin'] or self.store.one('SELECT 1 AS yes FROM transfer_owners WHERE transfer_id=? AND user_id=?',(tid,user['id'])))
        if not allowed:raise AppError('TASK_NOT_FOUND','任务不存在',404)
        return task

    @staticmethod
    def public(task):
        return {k:task[k] for k in ('id','title','work_id','infohash','state','progress','error_code','error_message','created_at','updated_at')}

    def get(self,user,tid):
        task=self.authorize(user,tid);out=self.public(task)
        out['files']=self.store.all('SELECT id,name,size,downloads FROM transfer_files WHERE transfer_id=? ORDER BY name',(tid,))
        out['events']=self.store.all('SELECT kind,message,created_at FROM transfer_events WHERE transfer_id=? ORDER BY id DESC LIMIT 15',(tid,))
        out['naming']=self.store.one('SELECT status,error FROM transfer_naming WHERE transfer_id=?',(tid,))
        if self.layout:
            row=self.layout.row(tid)
            out['organization']={key:row[key] for key in ('status','series_name','error')} if row else None
        for f in out['files']:f['download_url']=f'/api/transfers/{tid}/download/{f["id"]}'
        return out

    def list(self,user):
        if user['is_admin']:rows=self.store.all('SELECT * FROM transfers ORDER BY created_at DESC LIMIT 300')
        else:rows=self.store.all('SELECT t.* FROM transfers t JOIN transfer_owners o ON o.transfer_id=t.id WHERE o.user_id=? ORDER BY t.created_at DESC LIMIT 300',(user['id'],))
        return [self.public(x) for x in rows]

    def poll(self,tid,force=False):
        with self.connection_lock:
            task=self.claim(tid,POLLABLE)
            if not task:return
            try:
                b,pan=self.scope(task)
                if not task['task_cid']:
                    raise AppError('SUBMIT_REQUIRES_REVIEW','提交结果不确定，需管理员核实专用目录后处理',409)
                cloud_done=task['state']=='WAITING_FILES'
                upstream=None
                if task['alist_task_id'] and not cloud_done:
                    try:upstream=self.alist.task_info(task['alist_task_id'])
                    except ProviderError as error:
                        if error.code!='ALIST_NOT_FOUND':raise
                        self.update(tid,alist_task_id=None)
                if upstream:
                    state=int(upstream.get('state',-1));self.update(tid,progress=min(100,max(0,float(upstream.get('progress') or 0))))
                    if state in ALIST_FAILED:raise AppError('CLOUD_TASK_FAILED','115 云下载未完成，请检查账号配额、资源可用性后重试',409)
                    cloud_done=state==ALIST_SUCCEEDED
                elif not cloud_done:
                    remote=pan.find_offline_task(task['infohash'],task['task_cid'])
                    if remote:
                        if int(remote.get('status',0))==-1:raise AppError('CLOUD_TASK_FAILED','115 云下载失败，请检查资源可用性',409)
                        cloud_done=int(remote.get('status',0))==2
                        self.update(tid,state='CLOUD_DOWNLOADING',progress=float(remote.get('percentDone') or 0))
                    elif task['state']=='SUBMIT_UNKNOWN':
                        # A successful empty lookup is not permission to retry an ambiguous POST.
                        self.update(tid,error_code='SUBMIT_RESULT_UNKNOWN',error_message='尚未确认提交结果，系统不会自动重复提交')
                if cloud_done:
                    self.update(tid,state='WAITING_FILES')
                    files=self.collect_files(task['task_path'])
                    if files:
                        with self.store.transaction() as c:
                            for f in files:c.execute('INSERT INTO transfer_files(id,transfer_id,path,name,size) VALUES(?,?,?,?,?) ON CONFLICT(transfer_id,path) DO UPDATE SET size=excluded.size',
                                (secrets.token_hex(16),tid,f['path'],f['name'],f['size']))
                        self.update(tid,state='FILES_READY',progress=100,error_code=None,error_message=None,attempts=0)
                        self.store.event(tid,'FILES_READY','115 转存完成，已通过 Alist 核对文件，可直接下载')
                    else:self.update(tid,state='WAITING_FILES',error_code=None,error_message='云下载完成，等待 Alist 刷新文件')
                self.update(tid,next_poll_at=time.time()+max(5,self.config.worker_interval),attempts=0)
            except AppError as e:
                attempts=task['attempts']+1
                if e.code in {'CLOUD_TASK_FAILED','TASK_SCOPE_MISMATCH','TASK_ACCOUNT_MISMATCH'}:self.update(tid,state='FAILED')
                if e.code=='COOKIE_EXPIRED':self.update(tid,state='AUTH_REQUIRED')
                self.update(tid,error_code=e.code,error_message=e.message,attempts=attempts,next_poll_at=time.time()+min(900,10*(2**min(attempts,6))))
            finally:self.update(tid,lease_until=0,lease_owner=None)

    def collect_files(self,task_path):
        results=[];queue=[(task_path,0)];seen=set()
        while queue:
            path,depth=queue.pop()
            if path in seen:continue
            seen.add(path)
            if depth>8:raise AppError('DIRECTORY_TOO_DEEP','任务目录层级过深，无法自动确认所有文件',409)
            for row in self.alist.list_files(path):
                name=str(row.get('name') or '')
                decoded=urllib.parse.unquote(name)
                if not name or decoded in {'.','..'} or any(x in decoded for x in ('/','\\','\0','\r','\n')):
                    raise AppError('UNSAFE_FILE_NAME','上游返回不安全的文件名',409)
                child=path+'/'+name
                if not child.startswith(task_path+'/'):raise AppError('UNSAFE_PATH','文件超出任务目录',403)
                if row.get('is_dir'):queue.append((child,depth+1))
                else:results.append({'path':child,'name':child[len(task_path)+1:],'size':int(row.get('size') or 0)})
                if len(results)+len(queue)>10000:raise AppError('TOO_MANY_FILES','任务文件数量超过自动校验上限',409)
        return results

    def download(self,user,tid,file_id,range_header=None):
        task=self.authorize(user,tid)
        if task['state']!='FILES_READY':raise AppError('FILES_NOT_READY','云下载尚未完成',409)
        f=self.store.one('SELECT * FROM transfer_files WHERE id=? AND transfer_id=?',(file_id,tid))
        if not f:raise AppError('FILE_NOT_FOUND','文件不存在',404)
        with self.connection_lock:
            _,pan=self.scope(task)
            layout=self.layout.row(tid) if self.layout else None
            if layout:
                if layout['status']!='PUBLISHED':raise AppError('FILES_ORGANIZING','文件整理中，请稍后再试',409)
                self.layout.check_file(task,pan,f,layout)
            if not f['path'].startswith(task['task_path']+'/') or '..' in f['path'].split('/'):
                raise AppError('UNSAFE_PATH','文件超出专用任务目录',403)
            stream=self.alist.proxy_stream(f['path'],range_header)
        return f,stream

    def retry(self,user,tid):
        task=self.authorize(user,tid)
        if task['state']=='SUBMIT_UNKNOWN':raise AppError('AMBIGUOUS_SUBMISSION','提交结果尚未确认，请刷新核查；不会自动重复提交',409)
        if task['state'] not in {'FAILED','AUTH_REQUIRED'}:raise AppError('NOT_RETRYABLE','此任务当前不需要重新提交',409)
        with self.connection_lock:
            self.scope(task)
            self.update(tid,state='NEW',attempts=0,next_poll_at=0,lease_until=0)
            self.submit(tid)
        return self.get(user,tid)

    def tick(self):
        self.reconcile_connection()
        if self.store.setting('update_maintenance')=='1':return
        binding=self.store.binding(False)
        if binding and binding['ready'] and self.layout and binding['root_name']!=self.store.setting('cloud_root_name','anime'):
            if self.store.setting('connection_error_code')=='COOKIE_EXPIRED':return
            try:
                with self.connection_lock:self.scope()
                with self.store.transaction() as db:db.execute("DELETE FROM settings WHERE key IN ('connection_error','connection_error_code')")
            except AppError as error:
                self.store.set_setting('connection_error',error.message);self.store.set_setting('connection_error_code',error.code);return
        # Persisted NEW tasks are safe to claim after a restart. An interrupted
        # SUBMITTING task is ambiguous and must never be silently sent again.
        with self.store.transaction() as c:
            c.execute("UPDATE transfers SET state=CASE WHEN task_cid IS NULL THEN 'NEW' ELSE 'SUBMIT_UNKNOWN' END,lease_until=0,error_code='WORKER_RESTARTED',error_message='提交过程中工作进程中断，正在核实上游结果' WHERE state='SUBMITTING' AND lease_until<?",(time.time(),))
        if not self.store.binding(False) or not self.store.binding(False)['ready']:return
        for row in self.store.all("SELECT id,state FROM transfers WHERE lease_until<? AND next_poll_at<=? AND state IN ('NEW','SUBMIT_UNKNOWN','CLOUD_DOWNLOADING','WAITING_FILES') ORDER BY created_at LIMIT 10",(time.time(),time.time())):
            if row['state']=='NEW':self.submit(row['id'])
            else:self.poll(row['id'])
