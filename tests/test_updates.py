import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from mikan115.config import Config
from mikan115.errors import AppError,ProviderError
from mikan115.store import Store
from mikan115.supervisor import Supervisor
from mikan115.updater import Updates,API,RAW,REQUIRED,REPOSITORY,atomic_json,blob_sha,managed_path,pinned_requirements,read_json
from tests.test_workflow import Harness


CHILD='''import os,json,threading,time,sqlite3
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
home=Path(os.environ['MIKAN_DATA_DIR'])/'updates'
boot=os.environ['MIKAN_BOOT_ID']
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  raw=json.dumps({'ok':True,'boot_id':boot}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=HTTPServer(('127.0.0.1',int(os.environ['MIKAN_PORT'])),Handler)
def control():
 while True:
  try:state=json.loads((home/'control.json').read_text())
  except (OSError,ValueError):state={}
  if state.get('boot_id')==boot and state.get('stop'):server.shutdown();return
  time.sleep(.05)
threading.Thread(target=control,daemon=True).start()
try:server.serve_forever(poll_interval=.05)
finally:server.server_close()
'''

FAIL_CHILD='''import os,sqlite3
from pathlib import Path
with sqlite3.connect(Path(os.environ['MIKAN_DATA_DIR'])/'mikan115.sqlite') as db:
 db.execute("UPDATE settings SET value='damaged' WHERE key='protected_value'")
raise SystemExit(2)
'''


class FakeGit:
    version='b'*40
    tree='c'*40
    def __init__(self,files):self.files=files;self.downloads=[];self.truncated=False;self.corrupt=False
    def json(self,method,url,**kwargs):
        if url==API+'/commits/main':return {'sha':self.version,'commit':{'tree':{'sha':self.tree}}}
        if url==API+'/git/trees/'+self.tree+'?recursive=1':return {'sha':self.tree,'truncated':self.truncated,'tree':[
            {'path':name,'type':'blob','mode':'100644','sha':blob_sha(raw),'size':len(raw)} for name,raw in self.files.items()]}
        raise AssertionError('Unexpected network destination')
    def bytes(self,method,url,**kwargs):
        prefix=RAW+'/'+self.version+'/'
        if not url.startswith(prefix):raise AssertionError('Unexpected download destination')
        name=url[len(prefix):];self.downloads.append(name)
        return b'corrupt' if self.corrupt else self.files[name]


class UpdateSnapshots(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.root=self.base/'app';self.root.mkdir()
        self.files={name:(CHILD.encode() if name=='run.py' else b'' if name=='requirements.txt' else b'# fixture\n' if name.endswith('.py') else b'public fixture') for name in REQUIRED}
        for name,raw in self.files.items():path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        self.config=Config(root=self.root,data=self.base/'private');self.store=Store(self.config.data)
        self.git=FakeGit(dict(self.files));self.updates=Updates(self.store,self.config,self.git)
    def tearDown(self):
        if getattr(self,'supervisor',None):self.assertTrue(self.supervisor.stop_child())
        self.updates.close();self.temp.cleanup()

    def test_only_changed_blobs_download_and_private_state_is_untouched(self):
        self.store.set_setting('protected_value',self.store.encrypt('artificial-secret'))
        before=self.store.path.read_bytes();key=(self.config.data/'secret.key').read_bytes()
        self.git.files['mikan115/server.py']=b'# upgraded module\n'
        self.git.files['.env']=b'private must be ignored';self.git.files['data/secret.key']=b'ignored'
        self.git.files['mikan115/../../escape.py']=b'ignored'
        result=self.updates.stage();target=self.updates.home/'versions'/self.git.version
        self.assertEqual(self.git.downloads,['mikan115/server.py']);self.assertEqual(result['downloaded_files'],1)
        self.assertEqual(result['reused_files'],len(REQUIRED)-1)
        self.assertEqual(self.store.path.read_bytes(),before);self.assertEqual((self.config.data/'secret.key').read_bytes(),key)
        self.assertFalse((target/'.env').exists());self.assertFalse((target/'data').exists())
        self.assertEqual((self.root/'mikan115/server.py').read_bytes(),self.files['mikan115/server.py'])

    def test_bad_hash_and_truncated_manifest_never_become_ready(self):
        self.git.files['mikan115/server.py']=b'# changed\n';self.git.corrupt=True
        with self.assertRaises(AppError):self.updates.stage()
        self.assertFalse((self.updates.home/'versions'/self.git.version).exists())
        self.assertEqual(list((self.updates.home/'staging').iterdir()),[])
        self.git.truncated=True
        with self.assertRaises(AppError):self.updates.check()

    def test_github_api_rate_limit_uses_pinned_public_release_manifest(self):
        published={'protocol':1,'repository':REPOSITORY,'version':self.git.version,
            'files':{name:{'sha':blob_sha(raw),'size':len(raw)} for name,raw in self.git.files.items()}}
        original=self.git.bytes
        def download(method,url,**kwargs):
            if url==RAW+'/main/seed/update-manifest.json':return json.dumps(published).encode()
            return original(method,url,**kwargs)
        with patch.object(self.git,'json',side_effect=ProviderError('RATE_LIMIT','artificial rate limit')),patch.object(self.git,'bytes',side_effect=download):
            self.assertEqual(self.updates.manifest()['version'],self.git.version)
            self.assertEqual(self.updates.stage()['staged'],self.git.version)
        self.assertFalse(managed_path('seed/update-manifest.json'))

    def test_code_deletions_are_reflected_in_next_snapshot(self):
        self.git.files['mikan115/removed.py']=b'# old\n';self.updates.stage()
        first=self.updates.home/'versions'/self.git.version
        self.assertTrue((first/'mikan115/removed.py').exists())
        self.config.root=first;del self.git.files['mikan115/removed.py'];self.git.version='d'*40
        self.assertTrue(self.updates.check()['available']);self.updates.stage()
        self.assertFalse((self.updates.home/'versions'/self.git.version/'mikan115/removed.py').exists())
        self.assertEqual(self.git.downloads,['mikan115/removed.py'])

    def test_symlink_files_and_case_collisions_are_rejected(self):
        self.git.files['mikan115/Server.py']=b'# duplicate case\n'
        with self.assertRaises(AppError):self.updates.manifest()
        for name in ['../run.py','/run.py','mikan115/a:stream.py','mikan115/NUL.py','mikan115/a.py.','seed/private.sqlite','data/x.py','static/../secret.key']:
            self.assertFalse(managed_path(name),name)

    def test_requirements_are_pinned_and_dependency_install_is_automatic(self):
        for raw in [b'--index-url http://evil',b'package @ http://evil/x',b'package>=1',b'-r /private/file']:
            with self.assertRaises(AppError):pinned_requirements(raw)
        self.git.files['requirements.txt']=b'example-package==1.2.3\n'
        with patch.object(Updates,'install_dependencies') as install:
            self.updates.stage();self.assertEqual(install.call_args.args[1],['example-package==1.2.3'])

    def test_incremental_text_reuse_accepts_windows_line_endings(self):
        (self.root/'mikan115/server.py').write_bytes(b'# fixture\r\n')
        self.updates.stage();self.assertEqual(self.git.downloads,[])

    def test_update_requires_supervisor_and_settings_validate_types(self):
        with self.assertRaises(AppError):self.updates.request()
        with self.assertRaises(AppError):self.updates.configure({'auto_update':'yes'})
        self.assertTrue(self.updates.configure({'auto_update':True})['auto_update'])

    def test_anime_layout_blocks_incompatible_code_rollback_and_updates(self):
        self.store.set_setting('library_directory_version','anime:private-library')
        self.assertFalse(self.updates.layout_compatible(self.root))
        module=self.root/'mikan115/library_paths.py';module.write_text("STORAGE_ROOT_NAME='anime'\n",encoding='utf-8')
        self.assertTrue(self.updates.layout_compatible(self.root))
        self.updates.stage()
        with patch.object(self.updates,'status',return_value={'supervised':True}),self.assertRaises(AppError) as error:self.updates.request()
        self.assertEqual(error.exception.code,'UPDATE_LAYOUT_INCOMPATIBLE')
        self.assertFalse((self.updates.home/'request.json').exists())
        atomic_json(self.updates.home/'current.json',{'version':'b'*40,'previous':{'version':'bundled'}})
        with patch.dict(os.environ,{'MIKAN_BUNDLED_ROOT':str(self.root)}):
            self.assertTrue(self.updates.status()['rollback_available'])

    def test_interrupted_activation_recovers_database_before_work_starts(self):
        supervisor=Supervisor(self.config);self.store.set_setting('protected_value','original');backup=supervisor.backup()
        self.store.set_setting('protected_value','partially migrated')
        atomic_json(supervisor.home/'activation.json',{'previous':{'version':'bundled'},'target':'b'*40,'backup':backup.name})
        supervisor.recover_interrupted()
        self.assertEqual(self.store.setting('protected_value'),'original');self.assertIsNone(supervisor.child)
        self.assertEqual(supervisor.current['version'],'bundled')
        self.assertFalse((supervisor.home/'activation.json').exists())

    def test_switch_waits_for_active_task_lease(self):
        self.updates.stage();supervisor=Supervisor(self.config)
        class ActiveChild:
            def poll(self):return None
        supervisor.child=ActiveChild()
        with self.store.transaction() as db:db.execute("INSERT INTO transfers(id,account_uid,infohash,magnet,title,state,created_at,updated_at,lease_until) VALUES('task','1','hash','magnet','title','CLOUD_DOWNLOADING',0,0,?)",(time.time()+60,))
        self.assertFalse(supervisor.activate({'kind':'apply','version':self.git.version}))
        self.assertEqual(read_json(supervisor.home/'status.json')['phase'],'waiting')
        self.assertEqual(supervisor.current['version'],'bundled')

    def test_missing_backup_never_creates_an_empty_database_or_replaces_current_data(self):
        supervisor=Supervisor(self.config);self.store.set_setting('protected_value','original')
        missing=supervisor.home/'backups'/'20261128-000000-00000000.sqlite'
        with self.assertRaises(AppError):supervisor.restore(missing)
        self.assertFalse(missing.exists());self.assertEqual(self.store.setting('protected_value'),'original')

    def test_atomic_status_replace_retries_transient_file_sharing_conflict(self):
        target=self.updates.home/'control.json';atomic_json(target,{'value':'old'})
        original=os.replace;attempts=[]
        def briefly_locked(source,destination):
            attempts.append(1)
            if len(attempts)<3:raise PermissionError('simulated sharing conflict')
            return original(source,destination)
        with patch('mikan115.updater.os.replace',side_effect=briefly_locked):atomic_json(target,{'value':'new'})
        self.assertEqual(len(attempts),3);self.assertEqual(read_json(target),{'value':'new'})
        self.assertEqual(list(self.updates.home.glob('control.json.*.tmp')),[])

    def start_supervisor_fixture(self):
        with socket.socket() as port:port.bind(('127.0.0.1',0));self.config.port=port.getsockname()[1]
        self.environment=patch.dict(os.environ,{'MIKAN_PORT':str(self.config.port)});self.environment.start();self.addCleanup(self.environment.stop)
        supervisor=Supervisor(self.config);self.supervisor=supervisor;supervisor.health_timeout=4;supervisor.stop_timeout=5
        supervisor.spawn('bundled');self.assertTrue(supervisor.await_health());supervisor.control(active=True)
        return supervisor

    def test_supervisor_switches_versions_and_keeps_database_key_and_downloads(self):
        supervisor=self.start_supervisor_fixture();self.git.files['mikan115/server.py']=b'# update\n';self.updates.stage()
        self.store.set_setting('protected_value','original');key=(self.config.data/'secret.key').read_bytes()
        library=self.config.data/'library'/'mikan'/'Series';library.mkdir(parents=True);(library/'S01E01.mp4').write_bytes(b'video')
        self.assertTrue(supervisor.activate({'kind':'apply','version':self.git.version}))
        self.assertEqual(supervisor.current['version'],self.git.version);self.assertTrue(supervisor.healthy())
        self.assertEqual(self.store.setting('protected_value'),'original');self.assertEqual((self.config.data/'secret.key').read_bytes(),key)
        self.assertEqual((library/'S01E01.mp4').read_bytes(),b'video')
        self.store.set_setting('protected_value','new user data')
        self.assertTrue(supervisor.activate({'kind':'rollback'}))
        self.assertEqual(supervisor.current['version'],'bundled');self.assertEqual(self.store.setting('protected_value'),'new user data')

    def test_failed_start_restores_partial_database_changes_and_previous_code(self):
        supervisor=self.start_supervisor_fixture();self.store.set_setting('protected_value','original')
        self.git.files['run.py']=FAIL_CHILD.encode();self.updates.stage()
        self.assertTrue(supervisor.activate({'kind':'apply','version':self.git.version}))
        self.assertEqual(supervisor.current['version'],'bundled');self.assertTrue(supervisor.healthy())
        self.assertEqual(self.store.setting('protected_value'),'original')
        self.assertEqual(self.store.setting('update_maintenance'),'0')
        self.assertEqual(read_json(self.updates.home/'status.json')['phase'],'rolled_back')


class UpdateAuthorization(Harness):
    def test_admin_and_csrf_required_for_update_actions(self):
        status,_,_=self.request('POST','/api/settings/updates',{'auto_update':True},csrf=False)
        self.assertEqual(status,403)
        self.request('POST','/api/admin/users',{'username':'ordinary','password':'artificial-password-123'})
        self.request('POST','/api/auth/login',{'username':'ordinary','password':'artificial-password-123'})
        for method,body in [('GET',None),('POST',{'auto_update':True}),('POST',{'action':'apply'})]:
            status,_,_=self.request(method,'/api/settings/updates',body);self.assertEqual(status,403)
