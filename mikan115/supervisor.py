"""Small launcher which switches verified code snapshots without a Docker socket."""
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

from .config import Config
from .errors import AppError
from .store import Store
from .updater import Updates,atomic_json,read_json


class Supervisor:
    health_timeout=60
    stop_timeout=90
    def __init__(self,config):
        self.config=config;self.store=Store(config.data);self.home=config.data/'updates'
        self.home.mkdir(exist_ok=True,mode=0o700);self.child=None;self.boot_id='';self.stopping=False
        self.current=read_json(self.home/'current.json') or {'version':'bundled'}

    def root(self,version):
        if version=='bundled':return self.config.root
        if not re.fullmatch('[a-f0-9]{40}',str(version)):raise AppError('UPDATE_MANIFEST','更新版本无效',409)
        path=self.home/'versions'/version
        if Updates.verify(path)['version']!=version:raise AppError('UPDATE_MANIFEST','更新版本不一致',409)
        return path

    def heartbeat(self):atomic_json(self.home/'supervisor.json',{'heartbeat':time.time(),'pid':os.getpid(),'protocol':1})
    def control(self,*,stop=False,active=False):atomic_json(self.home/'control.json',{'boot_id':self.boot_id,'stop':stop,'active':active})
    def status(self,**fields):atomic_json(self.home/'status.json',{**read_json(self.home/'status.json'),**fields})

    def spawn(self,version):
        root=self.root(version);self.boot_id=secrets.token_hex(16);self.control()
        env={**os.environ,'MIKAN_SUPERVISED':'1','MIKAN_BOOT_ID':self.boot_id,'MIKAN_RUNNING_VERSION':version,
             'MIKAN_DATA_DIR':str(self.config.data.resolve()),'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
        self.child=subprocess.Popen([sys.executable,str(root/'run.py'),'--serve-child'],cwd=root,env=env,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))

    def healthy(self):
        host='127.0.0.1' if self.config.host in {'0.0.0.0','::'} else self.config.host
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://{host}:{self.config.port}/api/health',timeout=1) as response:
                result=json.loads(response.read(4096))
                return result.get('ok') is True and result.get('boot_id')==self.boot_id
        except (OSError,ValueError):return False

    def await_health(self):
        deadline=time.monotonic()+self.health_timeout;consecutive=0
        while time.monotonic()<deadline and not self.stopping:
            self.heartbeat()
            if self.child.poll() is not None:return False
            consecutive=consecutive+1 if self.healthy() else 0
            if consecutive>=3:return True
            time.sleep(.3)
        return False

    def stop_child(self):
        if not self.child or self.child.poll() is not None:return True
        self.control(stop=True);deadline=time.monotonic()+self.stop_timeout
        while self.child.poll() is None and time.monotonic()<deadline:
            self.heartbeat();time.sleep(.2)
        # A timed-out process is never force-killed and never runs beside a
        # replacement. Its durable jobs remain owned by the original process.
        return self.child.poll() is not None

    def backup(self):
        folder=self.home/'backups';folder.mkdir(exist_ok=True,mode=0o700)
        path=folder/(time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)+'.sqlite')
        with contextlib.closing(sqlite3.connect(self.store.path)) as source,contextlib.closing(sqlite3.connect(path)) as target:source.backup(target)
        path.chmod(0o600);return path

    def restore(self,path):
        path=Path(path).resolve();allowed=(self.home/'backups').resolve()
        if path.parent!=allowed or not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}\.sqlite',path.name) or not path.is_file():raise AppError('UPDATE_BACKUP','备份文件无效')
        # SQLite backup replaces database pages, including removal of partial
        # migrations. The stable encryption key is never replaced.
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
            if source.execute('PRAGMA quick_check').fetchone()[0]!='ok' or not source.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():raise AppError('UPDATE_BACKUP','备份未通过校验')
            with contextlib.closing(sqlite3.connect(self.store.path)) as target:source.backup(target)

    def recover_interrupted(self):
        interrupted=read_json(self.home/'activation.json')
        if not interrupted:return
        self.restore(self.home/'backups'/str(interrupted.get('backup','')))
        self.current=interrupted['previous'];self.root(self.current['version'])
        atomic_json(self.home/'current.json',self.current)
        (self.home/'activation.json').unlink(missing_ok=True);(self.home/'request.json').unlink(missing_ok=True)
        self.status(phase='rolled_back',error='更新中断，已恢复原版本')

    def activate(self,request):
        previous=dict(self.current);rollback=request.get('kind')=='rollback'
        desired=(previous.get('previous') or {}).get('version') if rollback else request.get('version')
        self.root(desired)
        if self.child and self.child.poll() is None:
            busy=self.store.one('SELECT 1 FROM transfers WHERE lease_until>? LIMIT 1',(time.time(),))
            if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_tasks'"):
                busy=busy or self.store.one("SELECT 1 FROM library_tasks WHERE lease_until>? AND status='DOWNLOADING' LIMIT 1",(time.time(),))
            if busy:self.status(phase='waiting',error=None);return False
        if not self.stop_child():
            self.status(phase='waiting',error='等待当前任务结束');return False
        backup=self.backup();self.status(phase='installing',error=None)
        atomic_json(self.home/'activation.json',{'previous':previous,'target':desired,'backup':backup.name})
        try:
            self.spawn(desired)
            if not self.await_health():raise AppError('UPDATE_START_FAILED','新版本启动失败，已恢复原版本',503)
            self.current={'version':desired,'previous':{'version':previous['version']},'backup':backup.name}
            atomic_json(self.home/'current.json',self.current)
            (self.home/'activation.json').unlink(missing_ok=True)
            self.store.set_setting('update_maintenance','0');self.control(active=True)
            self.status(phase='current',available=False,error=None,staged=None)
        except Exception:
            if not self.stop_child():
                self.status(phase='waiting',error='等待更新进程退出');return False
            self.restore(backup);self.current=previous;atomic_json(self.home/'current.json',previous)
            (self.home/'activation.json').unlink(missing_ok=True)
            self.store.set_setting('update_maintenance','0');self.spawn(previous['version'])
            if self.await_health():self.control(active=True)
            self.status(phase='rolled_back',error='新版本启动失败，已恢复原版本')
        (self.home/'request.json').unlink(missing_ok=True);return True

    def run(self):
        old=read_json(self.home/'supervisor.json')
        if old.get('heartbeat',0)>time.time()-15 and old.get('pid')!=os.getpid():raise SystemExit('An update supervisor is already running for this data directory.')
        self.heartbeat()
        try:
            self.recover_interrupted()
            self.spawn(self.current['version'])
            if not self.await_health():raise SystemExit('Application did not become ready; runtime data retained.')
            # On a host restart, interrupted activation is retried before
            # admitting work. A single child owns all jobs throughout.
            if not read_json(self.home/'request.json'):self.store.set_setting('update_maintenance','0');self.control(active=True)
            while not self.stopping:
                self.heartbeat();request=read_json(self.home/'request.json')
                if request:
                    try:self.activate(request)
                    except AppError as error:
                        self.status(phase='error',error=error.message);(self.home/'request.json').unlink(missing_ok=True)
                        self.store.set_setting('update_maintenance','0');self.control(active=True)
                elif self.child.poll() is not None:
                    self.spawn(self.current['version'])
                    if self.await_health():self.control(active=True)
                    else:raise SystemExit('Application stopped; runtime data retained.')
                time.sleep(.5)
        finally:
            self.stop_child();(self.home/'supervisor.json').unlink(missing_ok=True)


def main():
    supervisor=Supervisor(Config.from_env())
    def stop(*_):supervisor.stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    supervisor.run()
