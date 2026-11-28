import contextlib
import gzip
import hashlib
import hmac
import http.cookies
import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import urllib.parse

from . import releases
from .alist import Alist
from .auth import Auth,public_user
from .batch import Batch
from .catalog import Catalog
from .config import Config
from .errors import AppError
from .library import Library
from .rss import RSS
from .store import Store
from .workflow import Workflow
from .metadata import Metadata
from .sonarr import Sonarr
from .organizer import Organizer
from .releases import VERSIONS


class Application:
    def __init__(self,config,*,store=None,alist=None,pan_factory=None,catalog=None):
        self.config=config;self.store=store or Store(config.data)
        self.auth=Auth(self.store,config);self.alist=alist or Alist(config,self.store)
        self.workflow=Workflow(self.store,config,self.alist,**({'pan_factory':pan_factory} if pan_factory else {}))
        self.catalog=catalog or Catalog(self.store,config)
        self.metadata=getattr(self.catalog,'metadata',None) or Metadata(self.store)
        self.rss=RSS(self.store,config,self.catalog,self.workflow)
        self.sonarr=Sonarr(self.store,config=config);self.organizer=Organizer(self.store,self.workflow,self.catalog,self.sonarr)
        self.library=Library(self.store,self.workflow,catalog=self.catalog)
        self.batch=Batch(self.catalog,self.workflow,self.library)
        if self.auth.setup_required() and not config.setup_token and not self.store.setting('setup_token'):
            self.store.set_setting('setup_token',secrets.token_urlsafe(32))
        self.stop_event=threading.Event();self.worker=None;self.library_worker=None

    def start_worker(self):
        if hasattr(self.catalog,'start_updates'):self.catalog.start_updates()
        self.rss.start()
        def run():
            while not self.stop_event.is_set():
                try:self.workflow.tick();self.organizer.tick()
                except Exception:
                    # Never print provider exception bodies; they can contain account cookies.
                    print('Worker iteration failed; durable task state retained.',flush=True)
                self.stop_event.wait(max(1,self.config.worker_interval))
        self.worker=threading.Thread(target=run,name='mikan115-worker',daemon=True);self.worker.start()
        def run_library():
            while not self.stop_event.is_set():
                try:self.rss.enqueue_downloads(self.library);self.library.tick()
                except Exception:print('Library worker failed; durable task state retained.',flush=True)
                self.stop_event.wait(max(1,self.config.worker_interval))
        self.library_worker=threading.Thread(target=run_library,name='mikan115-library',daemon=True);self.library_worker.start()

    def close(self):
        self.stop_event.set()
        if self.worker:self.worker.join(timeout=25)
        if self.library_worker:self.library_worker.join(timeout=2)
        self.rss.close()
        if hasattr(self.catalog,'close'):self.catalog.close()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,app):
        self.app=app;super().__init__(address,Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version='Mikan115/0.1'
    protocol_version='HTTP/1.1'
    def log_message(self,*args):
        pass # No request paths, query strings, account identifiers or bodies in access logs.

    @property
    def app(self):return self.server.app

    def headers_common(self,cache_control='no-store'):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Cache-Control',cache_control)
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def send_json(self,data,status=200,cookie=None):
        raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
        raw,encoding=self.encode_body(raw)
        self.send_response(status);self.headers_common();self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Vary','Accept-Encoding')
        if encoding:self.send_header('Content-Encoding',encoding)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(raw)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def encode_body(self,raw):
        for item in self.headers.get('Accept-Encoding','').split(','):
            parts=[x.strip() for x in item.split(';')]
            if parts[0].lower()!='gzip':continue
            try:q=next((float(x.split('=',1)[1]) for x in parts[1:] if x.startswith('q=')),1)
            except (ValueError,IndexError):q=0
            if q>0 and len(raw)>=1024:return gzip.compress(raw,compresslevel=5,mtime=0),'gzip'
            break
        return raw,None

    def session_token(self):
        cookie=http.cookies.SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except http.cookies.CookieError:return ''
        return cookie['mikan115_session'].value if 'mikan115_session' in cookie else ''

    def session(self,admin=False,csrf=False):
        session=self.app.auth.session(self.session_token())
        if not session:raise AppError('LOGIN_REQUIRED','请先登录本地账号',401)
        if admin and not session['user']['is_admin']:raise AppError('ADMIN_REQUIRED','需要管理员权限',403)
        if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):raise AppError('CSRF_REQUIRED','页面会话已变化，请刷新后重试',403)
        return session['user']

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):raise AppError('UNSUPPORTED_BODY','不支持此请求传输方式',400)
        try:size=int(self.headers.get('Content-Length','0'))
        except ValueError:raise AppError('INVALID_BODY','请求内容长度不正确')
        if size<0 or size>32768:raise AppError('BODY_TOO_LARGE','请求内容过大',413)
        if self.headers.get_content_type()!='application/json':raise AppError('JSON_REQUIRED','请使用 JSON 请求',415)
        try:data=json.loads(self.rfile.read(size))
        except (ValueError,UnicodeError):raise AppError('INVALID_JSON','请求内容格式不正确')
        if not isinstance(data,dict):raise AppError('INVALID_JSON','请求内容须为对象')
        return data

    def ensure_origin(self):
        origin=self.headers.get('Origin')
        if origin:
            p=urllib.parse.urlsplit(origin)
            if p.netloc!=self.headers.get('Host','') or p.scheme not in {'http','https'}:
                raise AppError('ORIGIN_REJECTED','不接受跨站操作',403)

    def route(self):
        p=urllib.parse.urlsplit(self.path);path=urllib.parse.unquote(p.path)
        if path.startswith(('/quest/','/mikan/')):path='/'+path.split('/',2)[2]
        query=urllib.parse.parse_qs(p.query)
        return path,{k:v[0] for k,v in query.items()}

    def cookie(self,token,clear=False):
        secure='; Secure' if self.app.config.secure_cookie else ''
        return f'mikan115_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else self.app.config.session_hours*3600}{secure}'

    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        try:
            path,q=self.route()
            if path=='/api/health':return self.send_json({'ok':True,'service':'mikan115','version':'0.1.0','real_credentials_required_for_live_validation':True})
            if path=='/api/auth/config':return self.send_json({'setup_required':self.app.auth.setup_required(),'registration_enabled':False,'auth_type':'local','cookie_login_for_115_only':True})
            if path=='/api/auth/me':
                s=self.app.auth.session(self.session_token())
                return self.send_json({'authenticated':bool(s),'user':s['user'] if s else None,'csrf_token':s['csrf'] if s else None})
            if path in ('/api/seasons','/api/config'):return self.send_json(self.app.catalog.seasons() if path.endswith('seasons') else {'download_provider':'115','approval_required':False,'local_accounts':True,'cloud_folder_scope':'dedicated'})
            if path in ('/api/mikan/weekdays','/api/season'):
                return self.send_json(self.app.catalog.schedule(q.get('year'),q.get('season')))
            if path=='/api/homepage':
                data=self.app.catalog.schedule();items=[x for g in data['days'] for x in g['items']]
                return self.send_json({'works':items,'count':len(items),'source':data['source']})
            if path=='/api/search':return self.send_json(self.app.catalog.search(q.get('q','')))
            if path=='/api/discovery/rankings':raise AppError('ROUTE_REMOVED','功能已移除',410)
            if path.startswith('/api/bangumi/'):
                ident=path[len('/api/bangumi/'):]
                if q.get('resources')=='1':
                    data=self.app.catalog.resources(ident);return self.send_json({'subgroups':data['groups'],'subgroupsCount':len(data['groups']),'source_required':data['source_required']})
                return self.send_json(self.app.catalog.work(ident))
            if path.startswith('/api/catalog/work/'):return self.send_json(self.app.catalog.work(path[len('/api/catalog/work/'):]))
            if path.startswith('/api/catalog/series/'):return self.send_json(self.app.catalog.work('series:'+path.rsplit('/',1)[1]))
            if path=='/api/resources':
                self.session();return self.send_json(self.app.catalog.resources(q.get('work_id',''),q.get('bangumi_id'),q.get('subgroup_id')))
            if path=='/api/favorite':
                user=self.session();wid=q.get('canonicalId','');found=self.app.store.one('SELECT 1 AS yes FROM favorites WHERE user_id=? AND work_id=?',(user['id'],wid))
                return self.send_json({'favorite':bool(found),'canonicalId':wid})
            if path=='/api/quota':
                s=self.app.auth.session(self.session_token());return self.send_json({'authenticated':bool(s),'used':0,'remaining':None,'limit':None,'approval_required':False})
            if path=='/api/library' and 'mikan_ids' in q:
                user=self.session()
                ids=q['mikan_ids'].split(',')
                if len(ids)>500:raise AppError('INVALID_LIBRARY_IDS','一次最多查询 500 部作品')
                return self.send_json(self.app.library.summary(user,list(dict.fromkeys(Batch.source_id(i) for i in ids))))
            if path in ('/api/transfers','/api/my-requests','/api/library'):
                user=self.session();tasks=self.app.workflow.list(user)
                if path=='/api/library':return self.send_json({'items':tasks,'series':[],'count':len(tasks),'source':'115'})
                return self.send_json({'tasks':tasks,'requests':tasks,'count':len(tasks)})
            m=re.fullmatch(r'/api/transfers/([a-f0-9]{32})',path)
            if m:return self.send_json(self.app.workflow.get(self.session(),m[1]))
            m=re.fullmatch(r'/api/transfers/([a-f0-9]{32})/download/([a-f0-9]{32})',path)
            if m:return self.send_download(self.session(),m[1],m[2])
            m=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/vps-download',path)
            if m:return self.send_json(self.app.library.task(self.session(),m[1]))
            if path=='/api/subscriptions':return self.send_json({'subscriptions':self.app.rss.list(self.session(),q.get('scope')=='all'),'settings':self.app.rss.settings()})
            if path=='/api/subscriptions/options':
                self.session();return self.send_json(self.app.rss.options(q.get('work_id',''),q.get('bangumi_id')))
            if path=='/api/subtitle-groups':
                self.session();term=str(q.get('q',''))[:80]
                return self.send_json({'groups':self.app.store.all('SELECT group_id id,name,count(DISTINCT bangumi_id) works FROM work_groups WHERE name LIKE ? GROUP BY group_id,name ORDER BY works DESC,name LIMIT 1000',('%'+term+'%',))})
            m=re.fullmatch(r'/api/subscriptions/([a-f0-9]{32})/history',path)
            if m:return self.send_json(self.app.rss.history(self.session(),m[1]))
            if path=='/api/settings/sonarr':
                self.session(admin=True);return self.send_json(self.app.sonarr.status(check=True))
            if path=='/api/settings/alist':
                self.session(admin=True);return self.send_json(self.app.alist.status(check=True))
            if path=='/api/settings/connection':
                self.session(admin=True);return self.send_json(self.app.workflow.connection_status())
            if path=='/api/settings/metadata':
                self.session(admin=True);return self.send_json(self.app.metadata.status())
            if path=='/api/admin/users':
                self.session(admin=True);rows=self.app.store.all('SELECT * FROM users ORDER BY id')
                return self.send_json({'users':[public_user(x) for x in rows]})
            m=re.fullmatch(r'/api/tmdb/title/(tv|movie)/(\d+)',path)
            if m:
                season=q.get('season')
                if season is not None and (m[1]!='tv' or not re.fullmatch(r'[0-9]{1,5}',season)):
                    raise AppError('INVALID_SEASON','季号不正确')
                season=int(season) if season is not None else None
                result=self.app.catalog.tmdb_work(m[1],m[2],season)
                if result:return self.send_json({'result':result,'source':'catalog'})
                if not self.app.metadata.status()['tmdb_configured']:return self.send_json({'result':{},'source':'catalog'})
                return self.send_json({'result':self.app.metadata.title(m[1],m[2],season),'source':'TMDB'})
            if path=='/api/tmdb/search':return self.send_json({'results':[]})
            if path.startswith('/api/tmdb/person/'):
                ident=path.rsplit('/',1)[-1]
                if not ident.isdigit():raise AppError('NOT_FOUND','人物不存在',404)
                result=self.app.catalog.person(ident)
                if self.app.metadata.status()['tmdb_configured']:
                    try:
                        live=self.app.metadata.call('/person/'+ident,{'language':'zh-CN','append_to_response':'combined_credits'})
                        if str(live.get('id'))==ident:result={**live,'profile_url':self.app.metadata.image(live.get('profile_path'),'w185')}
                    except AppError:
                        if not result:raise
                return self.send_json({'result':result})
            if path.startswith('/api/'):
                raise AppError('ROUTE_REMOVED','此旧流程已移除，请使用直接转存任务',410)
            return self.send_static(path)
        except AppError as e:self.send_json({'error':e.message,'message':e.message,'code':e.code},e.status)
        except (BrokenPipeError,ConnectionError):self.close_connection=True
        except Exception:self.send_json({'error':'服务暂时无法处理请求','code':'INTERNAL_ERROR'},500)

    def do_POST(self):
        try:
            # Consume the bounded body before rejecting its origin, so the
            # HTTP connection cannot retain unread JSON as a next request.
            path,q=self.route();data=self.read_json();self.ensure_origin()
            if path=='/api/auth/setup':
                user=self.app.auth.create_user(data.get('username'),data.get('password'),admin=True,first=True,setup_token=data.get('setup_token',''))
                return self.send_json({'success':True,'user':user},201)
            if path=='/api/auth/login':
                token,csrf,user=self.app.auth.login(data.get('username',''),data.get('password',''),self.client_address[0])
                return self.send_json({'success':True,'authenticated':True,'user':user,'csrf_token':csrf},cookie=self.cookie(token))
            if path.startswith('/api/auth/') and path not in ('/api/auth/logout','/api/auth/password/change'):
                raise AppError('ROUTE_REMOVED','请使用本地账号登录',410)
            user=self.session(csrf=True)
            if path=='/api/auth/logout':
                self.app.auth.logout(self.session_token());return self.send_json({'success':True},cookie=self.cookie('',True))
            if path=='/api/auth/password/change':
                self.app.auth.change_password(user['id'],str(data.get('old_password') or data.get('oldPassword') or ''),str(data.get('new_password') or data.get('newPassword') or ''))
                return self.send_json({'success':True,'reauthenticate':True},cookie=self.cookie('',True))
            if path=='/api/settings/connection':
                self.session(admin=True)
                allowed={'cookie','cookie_source','alist_password','alist_token'}
                if set(data)-allowed:raise AppError('INVALID_CONNECTION_FIELDS','连接设置包含不允许的字段')
                return self.send_json(self.app.workflow.bind(data.get('cookie',''),cookie_source=data.get('cookie_source'),alist_password=data.get('alist_password',''),alist_token=data.get('alist_token','')))
            if path=='/api/settings/metadata':
                self.session(admin=True)
                if set(data)-{'tmdb_credential','clear_tmdb'}:raise AppError('INVALID_METADATA_FIELDS','设置无效')
                return self.send_json(self.app.metadata.configure(data.get('tmdb_credential',''),clear=data.get('clear_tmdb') is True))
            if path=='/api/settings/sonarr':
                self.session(admin=True);return self.send_json(self.app.sonarr.configure(data))
            if path=='/api/settings/alist':
                self.session(admin=True);return self.send_json(self.app.workflow.configure_alist(data))
            if path=='/api/settings/following':
                self.session(admin=True);return self.send_json(self.app.rss.configure(data))
            if path=='/api/library/actions':
                return self.send_json(self.app.batch.action(user,data),202 if 'digest' in data else 200)
            if path=='/api/library/downloads':
                ids=data.get('task_ids')
                if set(data)!={'task_ids'} or not isinstance(ids,list) or not 1<=len(ids)<=100 or any(
                        not isinstance(tid,str) or not re.fullmatch('[a-f0-9]{32}',tid) for tid in ids):
                    raise AppError('INVALID_LIBRARY_TASKS','请选择要下载的转存任务')
                tasks=[];failed=[]
                for tid in dict.fromkeys(ids):
                    try:tasks.append(self.app.library.enqueue(user,tid,{}))
                    except AppError as error:failed.append({'transfer_id':tid,'code':error.code,'message':error.message})
                return self.send_json({'tasks':tasks,'failed':failed},202)
            m=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/vps-download',path)
            if m:
                if set(data)-{'file_ids'}:raise AppError('INVALID_LIBRARY_FILES','请选择要下载的文件')
                return self.send_json(self.app.library.enqueue(user,m[1],data),202)
            if path=='/api/transfers':
                if set(data)&{'cid','folder_id','wp_path_id','path','mount_path','task_path','root_cid'}:raise AppError('FOLDER_OVERRIDE_FORBIDDEN','普通转存请求不能指定或改变目标目录',403)
                if 'release_ids' in data:
                    if set(data)-{'mikan_id','release_ids','rules'}:raise AppError('INVALID_RELEASES','批量转存参数无效')
                    return self.send_json(self.app.batch.selected(user,data.get('mikan_id'),data['release_ids'],data.get('rules')),202)
                naming=None
                if data.get('magnet'):
                    magnet,title,work_id=data['magnet'],data.get('title',''),data.get('work_id','')
                else:
                    magnet,title,work_id=self.app.catalog.resolve_resource(data)
                    bid=str(data.get('bangumi_id') or (work_id if str(work_id).isdigit() else ''))
                    if bid.isdigit():
                        naming={'work_id':work_id,'bangumi_id':bid,'resource_hash':str(data.get('resource_hash') or '').lower(),
                                'rules':{},'release':releases.parse(title)}
                task=self.app.workflow.create(user,magnet,title,work_id,naming=naming)
                return self.send_json({'success':True,'approval_required':False,'task':task},202)
            m=re.fullmatch(r'/api/transfers/([a-f0-9]{32})/(refresh|retry)',path)
            if m:
                self.app.workflow.authorize(user,m[1])
                if m[2]=='retry':task=self.app.workflow.retry(user,m[1])
                else:self.app.workflow.poll(m[1],True);task=self.app.workflow.get(user,m[1])
                return self.send_json({'task':task})
            if path=='/api/subscriptions':return self.send_json(self.app.rss.create(user,data),201)
            if path=='/api/subscriptions/options':
                prepared=self.app.rss.prepare(data)
                if prepared['source_kind']=='personal':return self.send_json({'source_kind':'personal','groups':[],'versions':VERSIONS})
                return self.send_json({**self.app.rss.options(prepared['work_id'],prepared['bangumi_id']),'source_subgroup_id':prepared['subgroup_id']})
            if path=='/api/subscriptions/preview':return self.send_json(self.app.rss.preview(user,data))
            m=re.fullmatch(r'/api/subscriptions/([a-f0-9]{32})/(preview|check)',path)
            if m:
                if m[2]=='preview':return self.send_json(self.app.rss.preview(user,data,m[1]))
                sub=self.app.rss.authorize(user,m[1]);self.app.store.binding();self.app.rss.poll(sub)
                return self.send_json(self.app.rss.history(user,m[1]))
            m=re.fullmatch(r'/api/subscriptions/([a-f0-9]{32})',path)
            if m:return self.send_json(self.app.rss.update(user,m[1],data))
            m=re.fullmatch(r'/api/transfers/([a-f0-9]{32})/rename',path)
            if m:
                if data.get('preview') is True:return self.send_json(self.app.organizer.public(self.app.organizer.preview(user,m[1],data.get('rename_rules'))))
                return self.send_json(self.app.organizer.apply(user,m[1],str(data.get('digest') or ''),data.get('rename_rules')))
            if path=='/api/admin/users':
                self.session(admin=True);created=self.app.auth.create_user(data.get('username'),data.get('password'),admin=False)
                return self.send_json({'user':created},201)
            if path=='/api/favorite':
                wid=str(data.get('canonicalId') or '');self.app.catalog.work(wid)
                with self.app.store.transaction() as c:
                    if data.get('favorite'):c.execute('INSERT OR IGNORE INTO favorites VALUES(?,?)',(user['id'],wid))
                    else:c.execute('DELETE FROM favorites WHERE user_id=? AND work_id=?',(user['id'],wid))
                return self.send_json({'favorite':bool(data.get('favorite')),'canonicalId':wid})
            raise AppError('ROUTE_REMOVED','此旧流程已移除，请使用直接转存任务',410)
        except AppError as e:self.send_json({'error':e.message,'message':e.message,'code':e.code},e.status)
        except (BrokenPipeError,ConnectionError):self.close_connection=True
        except Exception:self.send_json({'error':'服务暂时无法处理请求','code':'INTERNAL_ERROR'},500)

    def send_static(self,path):
        if '..' in path.split('/') or '\\' in path or '\0' in path:raise AppError('NOT_FOUND','页面不存在',404)
        root=self.app.config.root
        is_asset=path.startswith(('/assets/','/static/','/provider-logos/'))
        if path.startswith('/assets/'):target=root/'static'/path.lstrip('/')
        elif path.startswith('/static/'):target=root/path.lstrip('/')
        elif path.startswith('/provider-logos/'):target=root/'static'/path.lstrip('/')
        else:target=root/'templates/index.html'
        target=target.resolve()
        allowed_root=(root/'static').resolve() if is_asset else (root/'templates').resolve()
        if not target.is_relative_to(allowed_root) or not target.is_file():raise AppError('NOT_FOUND','资源不存在',404)
        if is_asset and target.suffix.lower() not in {'.js','.css','.html','.svg','.png','.jpg','.jpeg','.webp','.ico','.woff','.woff2','.ttf'}:
            raise AppError('NOT_FOUND','资源不存在',404)
        raw=target.read_bytes();etag='W/"'+hashlib.sha256(raw).hexdigest()+'"'
        cache='public, max-age=0, must-revalidate' if is_asset else 'no-store'
        if is_asset and self.headers.get('If-None-Match')==etag:
            self.send_response(304);self.headers_common(cache);self.send_header('ETag',etag);self.send_header('Vary','Accept-Encoding');self.end_headers();return
        raw,encoding=self.encode_body(raw);self.send_response(200);self.headers_common(cache)
        if is_asset:self.send_header('ETag',etag)
        self.send_header('Vary','Accept-Encoding')
        if encoding:self.send_header('Content-Encoding',encoding)
        mime=mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if target.suffix=='.js':mime='text/javascript'
        self.send_header('Content-Type',mime+('; charset=utf-8' if mime.startswith('text/') else ''))
        self.send_header('Content-Length',str(len(raw)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def send_download(self,user,tid,fid):
        range_header=self.headers.get('Range')
        if range_header and not re.fullmatch(r'bytes=\d*-\d*',range_header):raise AppError('INVALID_RANGE','仅支持单段字节范围',416)
        f,upstream=self.app.workflow.download(user,tid,fid,range_header)
        completed=False;total=0
        try:
            status=upstream.status
            if status not in (200,206):raise AppError('DOWNLOAD_UPSTREAM_FAILED','Alist 未返回可下载文件',502)
            self.send_response(status);self.headers_common()
            self.send_header('Content-Type',upstream.headers.get('Content-Type') or 'application/octet-stream')
            name=Path(f['name']).name.replace('\\','_').replace('\r','').replace('\n','')
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(name,safe=''))
            length=upstream.headers.get('Content-Length')
            if length and length.isdigit():self.send_header('Content-Length',length)
            else:self.send_header('Connection','close');self.close_connection=True
            if upstream.headers.get('Content-Range'):self.send_header('Content-Range',upstream.headers['Content-Range'])
            self.send_header('Accept-Ranges','bytes');self.end_headers()
            if self.command=='HEAD':return
            while chunk:=upstream.read(1024*256):self.wfile.write(chunk);total+=len(chunk)
            completed=not length or total==int(length)
        except (BrokenPipeError,ConnectionResetError,OSError):self.close_connection=True
        finally:
            upstream.close()
            if self.command!='HEAD':
                if completed:
                    with self.app.store.transaction() as c:c.execute('UPDATE transfer_files SET downloads=downloads+1 WHERE id=?',(fid,))
                self.app.store.event(tid,'DOWNLOAD_STREAM_FINISHED' if completed else 'DOWNLOAD_STREAM_INTERRUPTED',f'Alist 下载流已发送 {total} 字节')


def main():
    config=Config.from_env();app=Application(config)
    if app.auth.setup_required():
        print('Administrator setup required.',flush=True)
    server=Server((config.host,config.port),app);app.start_worker()
    print(f'Mikan115 listening on {config.host}:{config.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.close();server.server_close()


if __name__=='__main__':main()
