"""Verified incremental snapshots from the project's fixed public GitHub source."""
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import secrets
import shutil
import threading
import time

from .errors import AppError,ProviderError
from .http_client import HTTP

REPOSITORY='yywyao/anime-115'
BRANCH='main'
API='https://api.github.com/repos/'+REPOSITORY
RAW='https://raw.githubusercontent.com/'+REPOSITORY
REQUIRED={'run.py','requirements.txt','mikan115/server.py','mikan115/supervisor.py','static/mikan115-ui.js','static/assets/mikan115-app.js','templates/index.html','seed/catalog.sqlite'}


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as file:json.dump(value,file,ensure_ascii=False);file.flush();os.fsync(file.fileno())
        temporary.chmod(0o600)
        # Windows readers and antivirus scanners may briefly deny replacement.
        # Keep the old complete document until the atomic replace succeeds.
        for attempt in range(20):
            try:os.replace(temporary,path);break
            except PermissionError:
                if attempt==19:raise
                time.sleep(.01*(attempt+1))
    finally:temporary.unlink(missing_ok=True)


def read_json(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {} if default is None else default


def blob_sha(raw):return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def managed_path(name):
    if not isinstance(name,str) or len(name)>240 or not re.fullmatch(r'[A-Za-z0-9_./-]+',name):return False
    path=PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name or any(p in {'','.','..'} or p.startswith('.') for p in path.parts):return False
    if any(p.casefold() in {'data','runtime','cache','logs','node_modules','__pycache__','source-reference'} for p in path.parts):return False
    if any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} or p.endswith((' ','.')) for p in path.parts):return False
    if name=='seed/update-manifest.json':return False
    if name in {'run.py','requirements.txt'}:return True
    if path.parts[0]=='mikan115':return name.endswith('.py')
    if path.parts[0] in {'static','templates'}:return path.suffix.lower() in {'.html','.js','.css','.svg','.png','.jpg','.jpeg','.webp','.woff','.woff2','.ico'}
    if path.parts[0]=='seed':return name=='seed/catalog.sqlite' or path.suffix.lower()=='.json'
    return False


def matched_bytes(path,sha):
    try:
        if path.is_symlink() or not path.is_file():return None
        raw=path.read_bytes()
    except OSError:return None
    if blob_sha(raw)==sha:return raw
    if path.suffix.lower() in {'.py','.js','.css','.html','.json','.txt','.svg'}:
        normalized=raw.replace(b'\r\n',b'\n')
        if blob_sha(normalized)==sha:return normalized
    return None


def pinned_requirements(raw):
    try:lines=[line.strip() for line in raw.decode('utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#')]
    except UnicodeError:raise AppError('UPDATE_REQUIREMENTS','依赖配置无效',409)
    if len(lines)>100 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*',line) for line in lines):
        raise AppError('UPDATE_REQUIREMENTS','更新依赖必须使用固定版本',409)
    return lines


class Updates:
    def __init__(self,store,config,http=None):
        self.store,self.config=store,config;self.http=http or HTTP(timeout=15)
        self.home=store.data/'updates';self.home.mkdir(exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.operation=threading.Lock();self.worker=None;self.stop_event=threading.Event();self.checker=None

    def state(self):return read_json(self.home/'status.json')
    def write(self,**fields):
        state={**self.state(),**fields};atomic_json(self.home/'status.json',state);return state

    def status(self):
        state=self.state();heartbeat=read_json(self.home/'supervisor.json')
        supervised=os.environ.get('MIKAN_SUPERVISED')=='1' and heartbeat.get('heartbeat',0)>time.time()-20
        current=read_json(self.home/'current.json')
        return {key:state.get(key) for key in ('phase','latest','checked_at','changed_files','downloaded_files','reused_files','error')}|{
            'current':os.environ.get('MIKAN_RUNNING_VERSION') or current.get('version') or 'bundled',
            'repository':'https://github.com/'+REPOSITORY,'branch':BRANCH,'available':bool(state.get('available')),
            'staged':state.get('staged'),'supervised':supervised,'rollback_available':bool(current.get('previous') and current['previous'].get('version')!=current.get('version')),
            'auto_check':self.store.setting('update_auto_check','1')=='1','auto_update':self.store.setting('update_auto_apply','0')=='1'}

    def manifest(self):
        try:commit=self.http.json('GET',API+'/commits/'+BRANCH)
        except ProviderError:return self.published_manifest()
        sha=commit.get('sha','');tree=((commit.get('commit') or {}).get('tree') or {}).get('sha','')
        if not re.fullmatch('[a-f0-9]{40}',sha) or not re.fullmatch('[a-f0-9]{40}',tree):raise AppError('UPDATE_MANIFEST','版本信息无效',502)
        try:result=self.http.json('GET',API+'/git/trees/'+tree+'?recursive=1')
        except ProviderError:return self.published_manifest()
        if result.get('truncated') or result.get('sha')!=tree:raise AppError('UPDATE_MANIFEST','版本目录不完整',502)
        return self.validate_manifest(sha,result.get('tree',[]))

    def published_manifest(self):
        try:published=json.loads(self.http.bytes('GET',RAW+'/'+BRANCH+'/seed/update-manifest.json',limit=1024*1024))
        except (ValueError,UnicodeError):raise AppError('UPDATE_MANIFEST','发布清单无效',502)
        if not isinstance(published,dict) or published.get('protocol')!=1 or published.get('repository')!=REPOSITORY or not isinstance(published.get('files'),dict):
            raise AppError('UPDATE_MANIFEST','发布清单无效',502)
        rows=[]
        for name,row in published['files'].items():
            if not managed_path(name) or not isinstance(row,dict):raise AppError('UPDATE_MANIFEST','发布清单包含不支持的文件',502)
            rows.append({'path':name,'sha':row.get('sha'),'size':row.get('size'),'type':'blob','mode':row.get('mode') or '100644'})
        return self.validate_manifest(published.get('version'),rows)

    @staticmethod
    def validate_manifest(sha,rows):
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{40}',sha):raise AppError('UPDATE_MANIFEST','版本信息无效',502)
        files={};case=set();size=0
        for row in rows:
            name=row.get('path')
            if not managed_path(name):continue
            if row.get('type')!='blob' or row.get('mode') not in {'100644','100755'} or not re.fullmatch('[a-f0-9]{40}',str(row.get('sha') or '')):
                raise AppError('UPDATE_MANIFEST','版本包含不支持的文件',502)
            if name.casefold() in case:raise AppError('UPDATE_MANIFEST','版本文件名冲突',502)
            length=row.get('size')
            if type(length) is not int or not 0<=length<=64*1024*1024:raise AppError('UPDATE_MANIFEST','版本文件过大',502)
            case.add(name.casefold());files[name]={'sha':row['sha'],'size':length};size+=length
        if not REQUIRED<=set(files) or size>128*1024*1024:raise AppError('UPDATE_MANIFEST','版本文件不完整或过大',502)
        return {'version':sha,'files':files}

    def check(self):
        with self.lock:
            manifest=self.manifest();changed=0
            for name,row in manifest['files'].items():changed+=matched_bytes(self.config.root/name,row['sha']) is None
            installed=read_json(self.config.root/'snapshot.json').get('files') or {}
            if not installed:
                installed={str(file.relative_to(self.config.root)).replace('\\','/'):{} for name in ('mikan115','static','templates','seed')
                    for file in (self.config.root/name).rglob('*') if file.is_file() and managed_path(str(file.relative_to(self.config.root)).replace('\\','/'))}
            changed+=len(set(installed)-set(manifest['files']))
            atomic_json(self.home/'latest.json',manifest)
            staged=self.state().get('staged')
            return self.write(phase='available' if changed else 'current',available=bool(changed),latest=manifest['version'],checked_at=time.time(),changed_files=changed,error=None,
                staged=staged if staged==manifest['version'] else None)

    def stage(self):
        with self.lock:
            state=self.check();manifest=read_json(self.home/'latest.json');version=manifest['version']
            target=self.home/'versions'/version
            if target.exists():
                self.verify(target,manifest);self.write(phase='ready',staged=version,error=None);return self.status()
            temporary=self.home/'staging'/secrets.token_hex(16);temporary.mkdir(parents=True,mode=0o700)
            downloaded=0;reused=0
            self.write(phase='downloading',error=None,downloaded_files=0,reused_files=0)
            try:
                for name,row in manifest['files'].items():
                    raw=matched_bytes(self.config.root/name,row['sha'])
                    if raw is None:
                        raw=self.http.bytes('GET',RAW+'/'+version+'/'+name,limit=row['size']+1)
                        if len(raw)!=row['size'] or blob_sha(raw)!=row['sha']:raise AppError('UPDATE_HASH_MISMATCH','更新文件校验失败',502)
                        downloaded+=1
                    else:reused+=1
                    destination=temporary/name;destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(raw)
                    if name.endswith('.py'):compile(raw,name,'exec')
                    self.write(downloaded_files=downloaded,reused_files=reused)
                requirements=pinned_requirements((temporary/'requirements.txt').read_bytes())
                if (temporary/'requirements.txt').read_bytes().replace(b'\r\n',b'\n')!=(self.config.root/'requirements.txt').read_bytes().replace(b'\r\n',b'\n'):
                    self.install_dependencies(temporary,requirements)
                elif (self.config.root/'.deps').is_dir():shutil.copytree(self.config.root/'.deps',temporary/'.deps')
                atomic_json(temporary/'snapshot.json',manifest)
                self.verify(temporary,manifest);target.parent.mkdir(parents=True,exist_ok=True);os.replace(temporary,target)
                self.write(phase='ready',staged=version,error=None)
            except BaseException:
                resolved=temporary.resolve();allowed=(self.home/'staging').resolve()
                if resolved.is_relative_to(allowed) and resolved!=allowed and temporary.exists():shutil.rmtree(temporary)
                raise
            return self.status()

    @staticmethod
    def install_dependencies(root,requirements):
        import subprocess,sys
        if not requirements:return
        result=subprocess.run([sys.executable,'-m','pip','--isolated','--disable-pip-version-check','install','--no-cache-dir','--only-binary=:all:','--index-url','https://pypi.org/simple','--target',str(root/'.deps'),*requirements],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
        if result.returncode:raise AppError('UPDATE_DEPENDENCIES','更新依赖安装失败，原版本已保留',502)

    @staticmethod
    def verify(root,manifest=None):
        root=Path(root);manifest=manifest or read_json(root/'snapshot.json')
        if not re.fullmatch('[a-f0-9]{40}',str(manifest.get('version') or '')) or not REQUIRED<=set(manifest.get('files') or {}):raise AppError('UPDATE_MANIFEST','更新目录无效',409)
        for name,row in manifest['files'].items():
            if not managed_path(name) or not (root/name).resolve().is_relative_to(root.resolve()) or matched_bytes(root/name,row['sha']) is None:raise AppError('UPDATE_HASH_MISMATCH','更新目录校验失败',409)
        return manifest

    def request(self,rollback=False):
        with self.lock:
            if not self.status()['supervised']:raise AppError('UPDATE_SUPERVISOR_REQUIRED','更新服务未启动，请通过 run.py 启动一次',409)
            current=read_json(self.home/'current.json')
            if rollback:
                version=(current.get('previous') or {}).get('version')
                if not version:raise AppError('UPDATE_NO_ROLLBACK','没有可恢复的版本',409)
            else:
                version=self.state().get('staged')
                if not version:raise AppError('UPDATE_NOT_STAGED','请先下载更新',409)
                self.verify(self.home/'versions'/version)
            self.store.set_setting('update_maintenance','1')
            atomic_json(self.home/'request.json',{'kind':'rollback' if rollback else 'apply','version':version,'created_at':time.time()})
            self.write(phase='waiting',error=None)
            return self.status()

    def configure(self,body):
        if set(body)-{'auto_check','auto_update'} or any(type(value) is not bool for value in body.values()):raise AppError('INVALID_SETTINGS','更新设置无效')
        for key,name in [('auto_check','update_auto_check'),('auto_update','update_auto_apply')]:
            if key in body:self.store.set_setting(name,'1' if body[key] else '0')
        return self.status()

    def run(self,action):
        if action not in {'check','download','apply','rollback'}:raise AppError('INVALID_UPDATE','更新操作无效')
        if not self.operation.acquire(blocking=False):raise AppError('UPDATE_BUSY','更新处理中',409)
        if action in {'apply','rollback'}:
            try:return self.request(action=='rollback')
            finally:self.operation.release()
        def work():
            try:self.check() if action=='check' else self.stage()
            except Exception as error:self.write(phase='error',error=error.message if isinstance(error,AppError) else '更新失败，原版本已保留')
            finally:self.operation.release()
        self.write(phase='checking' if action=='check' else 'downloading',error=None)
        self.worker=threading.Thread(target=work,name='project-update',daemon=True);self.worker.start();return self.status()

    def start(self):
        if self.checker:return
        def check_loop():
            while not self.stop_event.wait(30):
                if not self.status()['supervised'] or not self.status()['auto_check']:continue
                if self.state().get('checked_at',0)>time.time()-21600:continue
                if not self.operation.acquire(blocking=False):continue
                try:
                    state=self.check()
                    if state.get('available') and self.status()['auto_update']:self.stage();self.request()
                except Exception as error:self.write(phase='error',checked_at=time.time()-19800,error=error.message if isinstance(error,AppError) else '检查更新失败')
                finally:self.operation.release()
        self.checker=threading.Thread(target=check_loop,name='update-check',daemon=True);self.checker.start()

    def close(self):
        self.stop_event.set()
        if self.checker:self.checker.join(timeout=1)
        if self.worker:self.worker.join(timeout=1)
