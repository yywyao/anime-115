import concurrent.futures
import hashlib
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from mikan115.alist import Alist
from mikan115.auth import password_hash,password_matches
from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.errors import AppError
from mikan115.pan115 import Pan115,normalize_cookie
from mikan115.server import Application,Server
from mikan115.store import Store
from mikan115.torrent import torrent_magnet
from mikan115.workflow import normalize_magnet
from tests.mock_services import State,MockServer,LocalOnlyHTTP,HASH,COOKIE,PAYLOAD


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.state=State();self.mock=MockServer(self.state).start()
        self.config=Config(root=Path(self.temp.name)/'isolated-app',data=Path(self.temp.name)/'state',
            alist_url=self.mock.base,alist_password='artificial-alist-password',mikan_url=self.mock.base,
            setup_token='artificial-private-setup-token',worker_interval=1)
        self.store=Store(self.config.data);self.alist=Alist(self.config,self.store,LocalOnlyHTTP(3))
        self.pan=lambda ck:Pan115(ck,LocalOnlyHTTP(3),my_base=self.mock.base+'/115-user',web_base=self.mock.base+'/115-web',offline_base=self.mock.base+'/115-offline')
        self.catalog=Catalog(self.store,self.config,LocalOnlyHTTP(3))
        self.app=Application(self.config,store=self.store,alist=self.alist,pan_factory=self.pan,catalog=self.catalog)
        self.server=Server(('127.0.0.1',0),self.app);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.jar=http.cookiejar.CookieJar();self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar));self.csrf=''
        self.request('POST','/api/auth/setup',{'username':'testadmin','password':'artificial-password-123','setup_token':self.config.setup_token})
        _,data,_=self.request('POST','/api/auth/login',{'username':'testadmin','password':'artificial-password-123'});self.user=data['user']

    def tearDown(self):
        self.app.close();self.server.shutdown();self.server.server_close();self.thread.join(timeout=5);self.mock.close();self.temp.cleanup()

    def request(self,method,path,data=None,headers=None,*,client=None,csrf=True,binary=False):
        h={'Content-Type':'application/json'}
        if csrf and self.csrf:h['X-CSRF-Token']=self.csrf
        h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,method=method,headers=h)
        try:response=(client or self.client).open(req,timeout=15)
        except urllib.error.HTTPError as e:response=e
        with response:
            raw=response.read();body=raw if binary else json.loads(raw)
            if isinstance(body,dict) and body.get('csrf_token'):self.csrf=body['csrf_token']
            return response.status,body,dict(response.headers)

    def bind(self):
        status,data,_=self.request('POST','/api/settings/connection',{'cookie':COOKIE,'cookie_source':'third_party'})
        self.assertEqual(status,200,data);self.assertTrue(data['ready']);return data

    def create(self,ident=HASH):
        status,data,_=self.request('POST','/api/transfers',{'magnet':'magnet:?xt=urn:btih:'+ident,'title':'Artificial episode'})
        self.assertEqual(status,202,data);return data['task']


class ClosedLoop(Harness):
    def test_without_ck_worker_never_contacts_alist_or_115(self):
        self.app.workflow.tick();self.app.rss.tick();self.assertEqual(self.state.calls,[])

    def test_web_resource_to_cloud_to_alist_download_bytes(self):
        self.bind()
        status,schedule,_=self.request('GET','/api/mikan/weekdays');self.assertEqual(status,200);self.assertEqual(schedule['source'],'live')
        item=next(i for d in schedule['days'] for i in d['items']);self.assertEqual(item['bangumiId'],'236')
        status,detail,_=self.request('GET','/api/bangumi/236?summary=1');self.assertEqual(detail['title'],'测试动漫')
        status,resources,_=self.request('GET','/api/resources?work_id=236');self.assertEqual(status,200)
        resource=resources['groups'][0]['resources'][0];self.assertEqual(resource['resourceHash'],HASH)
        status,data,_=self.request('POST','/api/transfers',{'work_id':'236','bangumi_id':'236','subgroup_id':'7','resource_hash':HASH})
        self.assertEqual(status,202,data);self.assertFalse(data['approval_required']);task=data['task'];self.assertEqual(task['state'],'CLOUD_DOWNLOADING')
        self.assertEqual(self.state.submit_count,1);root=self.store.binding()['root_cid'];self.assertNotIn(root,{'0','1'})
        remote=self.state.cloud[HASH];self.assertEqual(self.state.folders[remote['wp_path_id']]['parent'],root)
        self.state.complete()
        _,fresh,_=self.request('POST',f'/api/transfers/{task["id"]}/refresh',{})
        self.assertEqual(fresh['task']['state'],'FILES_READY',fresh);file=fresh['task']['files'][0]
        status,content,headers=self.request('GET',file['download_url'],binary=True)
        self.assertEqual(status,200);self.assertEqual(hashlib.sha256(content).digest(),hashlib.sha256(PAYLOAD).digest())
        self.assertNotIn('Cookie',headers);self.assertNotIn('raw_url',headers);self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(self.state.folders['1'],{'name':'DailyFiles','parent':'0'})
        self.assertTrue(all(x==COOKIE for x in self.state.cookies))

    def test_duplicate_clicks_and_concurrent_requests_submit_once(self):
        self.bind()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs=list(pool.map(lambda _:self.app.workflow.create(self.user,'magnet:?xt=urn:btih:'+HASH,'one episode'),range(12)))
        self.assertEqual(len({x['id'] for x in jobs}),1);self.assertEqual(self.state.submit_count,1)

    def test_incomplete_cloud_task_cannot_be_downloaded(self):
        self.bind();task=self.create();self.app.workflow.poll(task['id'])
        self.assertEqual(self.app.workflow.get(self.user,task['id'])['files'],[])
        status,data,_=self.request('GET',f'/api/transfers/{task["id"]}/download/'+('f'*32),binary=True)
        self.assertEqual(status,409)

    def test_range_download_keeps_alist_proxy_and_exact_bytes(self):
        self.bind();task=self.create();self.state.complete();self.app.workflow.poll(task['id'])
        file=self.app.workflow.get(self.user,task['id'])['files'][0]
        status,body,headers=self.request('GET',file['download_url'],headers={'Range':'bytes=0-8'},binary=True)
        self.assertEqual(status,206);self.assertEqual(body,PAYLOAD[:9]);self.assertEqual(headers['Content-Range'],f'bytes 0-8/{len(PAYLOAD)}')

    def test_mutated_alist_root_fails_closed_before_transfer(self):
        self.bind();s=next(iter(self.state.storages.values()));add=json.loads(s['addition']);add['root_folder_id']='1';s['addition']=json.dumps(add)
        task=self.create();self.assertEqual(task['state'],'FAILED');self.assertEqual(task['error_code'],'ALIST_STORAGE_CHANGED');self.assertEqual(self.state.submit_count,0)

    def test_no_caller_selected_destination(self):
        self.bind()
        status,data,_=self.request('POST','/api/transfers',{'magnet':'magnet:?xt=urn:btih:'+HASH,'folder_id':'1'})
        self.assertEqual(status,403);self.assertEqual(data['code'],'FOLDER_OVERRIDE_FORBIDDEN');self.assertEqual(self.state.submit_count,0)

    def test_same_magnet_in_daily_folder_is_not_moved_or_overwritten(self):
        self.bind();self.state.cloud[HASH]={'info_hash':HASH,'wp_path_id':'1','status':1,'percentDone':0}
        task=self.create();self.assertEqual(task['state'],'FAILED');self.assertEqual(task['error_code'],'REMOTE_TASK_FOLDER_CONFLICT')
        self.assertEqual(self.state.cloud[HASH]['wp_path_id'],'1');self.assertEqual(self.state.submit_count,0)

    def test_alist_cookie_changed_to_another_session_is_detected(self):
        self.bind();s=next(iter(self.state.storages.values()));a=json.loads(s['addition']);a['cookie']=COOKIE.replace('fictional-session','another-session');s['addition']=json.dumps(a)
        task=self.create();self.assertEqual(task['error_code'],'ALIST_COOKIE_MISMATCH');self.assertEqual(self.state.submit_count,0)

    def test_private_mount_permission_tamper_is_detected(self):
        self.bind();meta=next(iter(self.state.metas.values()));self.assertTrue(meta['p_sub']);self.assertTrue(meta['password']);meta['password']=''
        task=self.create();self.assertEqual(task['error_code'],'ALIST_META_CHANGED');self.assertEqual(self.state.submit_count,0)

    def test_saved_file_path_cannot_escape_dedicated_task(self):
        self.bind();task=self.create();self.state.complete();self.app.workflow.poll(task['id'])
        f=self.app.workflow.get(self.user,task['id'])['files'][0]
        with self.store.transaction() as c:c.execute('UPDATE transfer_files SET path=? WHERE id=?',('/DailyFiles/daily-secret.txt',f['id']))
        status,_,_=self.request('GET',f['download_url'],binary=True);self.assertEqual(status,403)

    def test_ambiguous_submit_is_reconciled_without_resubmission(self):
        self.bind();self.state.malformed_submit=True;task=self.create();self.assertEqual(task['state'],'SUBMIT_UNKNOWN')
        self.app.workflow.poll(task['id']);self.assertEqual(self.state.submit_count,1)
        self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'CLOUD_DOWNLOADING')
        self.state.complete();self.app.workflow.poll(task['id']);self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'FILES_READY')

    def test_unknown_submit_without_remote_record_never_resends(self):
        self.bind();task=self.create();self.state.cloud.clear()
        self.app.workflow.update(task['id'],state='SUBMIT_UNKNOWN',alist_task_id=None)
        for _ in range(3):self.app.workflow.poll(task['id'])
        self.assertEqual(self.state.submit_count,1);self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'SUBMIT_UNKNOWN')
        status,_,_=self.request('POST',f'/api/transfers/{task["id"]}/retry',{});self.assertEqual(status,409)

    def test_restart_recovers_inflight_without_new_cloud_submission(self):
        self.bind();task=self.create();self.app.workflow.update(task['id'],state='SUBMITTING',alist_task_id=None,lease_until=0,next_poll_at=0)
        self.app.workflow.tick();self.assertEqual(self.state.submit_count,1);self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'CLOUD_DOWNLOADING')

    def test_crash_before_persisted_folder_safely_resumes_preparation(self):
        self.bind();tid='e'*32
        with self.store.transaction() as c:
            c.execute("INSERT INTO transfers(id,account_uid,infohash,magnet,title,state,created_at,updated_at) VALUES(?,?,?,?,?,'SUBMITTING',?,?)",(tid,'501',HASH,'magnet:?xt=urn:btih:'+HASH,'Interrupted preparation',time.time(),time.time()))
            c.execute('INSERT INTO transfer_owners VALUES(?,?)',(tid,self.user['id']))
        self.app.workflow.tick();self.assertEqual(self.state.submit_count,1);self.assertEqual(self.app.workflow.get(self.user,tid)['state'],'CLOUD_DOWNLOADING')

    def test_confirmed_completion_survives_upstream_task_record_cleanup(self):
        self.bind();task=self.create();self.state.complete();saved=dict(self.state.files);self.state.files.clear()
        self.app.workflow.poll(task['id']);self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'WAITING_FILES')
        self.state.tasks.clear();self.state.cloud.clear();self.state.files=saved
        self.app.workflow.poll(task['id']);self.assertEqual(self.app.workflow.get(self.user,task['id'])['state'],'FILES_READY')

    def test_expired_cookie_pauses_and_never_qr_logs_in(self):
        self.bind();self.state.expired=True;task=self.create();self.assertEqual(task['state'],'AUTH_REQUIRED')
        for _ in range(2):self.app.workflow.tick()
        self.assertEqual(self.state.submit_count,0)
        self.assertFalse(any('qrcode' in p or 'passport' in p for _,p,_,_ in self.state.calls))

    def test_cloud_rejection_redacts_cookie(self):
        self.bind();self.state.fail_submit=True;task=self.create();self.assertEqual(task['state'],'FAILED')
        self.assertNotIn(COOKIE,json.dumps(task));self.assertNotIn('fictional-session',json.dumps(task))

    def test_path_traversal_from_file_listing_not_exposed(self):
        self.bind();task=self.create();self.state.complete();self.state.bad_file_name=True;self.app.workflow.poll(task['id'])
        data=self.app.workflow.get(self.user,task['id']);self.assertNotEqual(data['state'],'FILES_READY');self.assertEqual(data['files'],[])

    def test_rss_only_new_items_direct_transfer_and_dedup(self):
        self.bind();status,data,_=self.request('POST','/api/subscriptions',{'bangumi_id':'236','subgroup_id':'7','title':'test RSS'})
        self.assertEqual(status,201,data);self.assertEqual(self.state.submit_count,0)
        sub=self.store.one('SELECT * FROM subscriptions WHERE id=?',(data['id'],))
        self.state.feed_hashes.append('b'*40);self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
        self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
        self.app.rss.toggle(self.user,sub['id'],False);self.state.feed_hashes.append('c'*40);self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)

    def test_cancel_rss_between_resolution_and_enqueue_prevents_new_task(self):
        self.bind();sub_result=self.app.rss.create(self.user,{'bangumi_id':'236','subgroup_id':'7'})
        sub=self.store.one('SELECT * FROM subscriptions WHERE id=?',(sub_result['id'],));self.state.feed_hashes.append('d'*40)
        arrived=threading.Event();resume=threading.Event();original=self.app.workflow.create;errors=[]
        def delayed(*args,**kwargs):
            arrived.set()
            if not resume.wait(5):raise RuntimeError('Test synchronization timed out')
            return original(*args,**kwargs)
        self.app.workflow.create=delayed
        def poll():
            try:self.app.rss.poll(sub)
            except Exception as e:errors.append(e)
        thread=threading.Thread(target=poll);thread.start();self.assertTrue(arrived.wait(5))
        self.app.rss.toggle(self.user,sub['id'],False);resume.set();thread.join(timeout=8)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(self.state.submit_count,0)

    def test_local_password_csrf_and_private_credentials(self):
        self.bind();stored=self.store.one('SELECT * FROM binding')['cookie_encrypted'];self.assertNotIn('UID=',stored);self.assertEqual(self.store.decrypt(stored),COOKIE)
        _,data,_=self.request('GET','/api/settings/connection');self.assertNotIn('cookie',data);self.assertNotIn(COOKIE,json.dumps(data))
        status,_,_=self.request('POST','/api/transfers',{'magnet':'magnet:?xt=urn:btih:'+HASH},csrf=False);self.assertEqual(status,403)
        status,_,_=self.request('POST','/api/transfers',{'magnet':'magnet:?xt=urn:btih:'+HASH},headers={'Origin':'https://another-site.invalid'});self.assertEqual(status,403)

    def test_other_user_cannot_read_or_download_private_task(self):
        self.bind();task=self.create();other=self.app.auth.create_user('anotheruser','different-test-password')
        with self.assertRaises(AppError) as ctx:self.app.workflow.get(other,task['id'])
        self.assertEqual(ctx.exception.status,404)
        jar=http.cookiejar.CookieJar();client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        _,data,_=self.request('POST','/api/auth/login',{'username':'anotheruser','password':'different-test-password'},client=client)
        status,_,_=self.request('GET','/api/settings/connection',client=client);self.assertEqual(status,403)
        status,_,_=self.request('GET',f'/api/transfers/{task["id"]}',client=client);self.assertEqual(status,404)

    def test_original_approval_and_media_login_routes_removed(self):
        self.bind()
        for path in ['/api/admin/requests/approve','/api/requests','/api/auth/emby/login']:
            status,_,_=self.request('POST',path,{})
            self.assertEqual(status,410,path)
        self.assertEqual(self.state.submit_count,0)

    def test_local_password_change_revokes_all_sessions(self):
        status,data,_=self.request('POST','/api/auth/password/change',{'old_password':'artificial-password-123','new_password':'new-artificial-password'})
        self.assertEqual(status,200,data)
        _,me,_=self.request('GET','/api/auth/me');self.assertFalse(me['authenticated'])

    def test_bootstrap_cannot_create_second_admin(self):
        status,_,_=self.request('POST','/api/auth/setup',{'username':'attacker','password':'artificial-pass-123','setup_token':self.config.setup_token})
        self.assertEqual(status,409)

    def test_invalid_cookie_and_root_storage_never_created(self):
        status,data,_=self.request('POST','/api/settings/connection',{'cookie':'UID=1; CID=x','cookie_source':'third_party'})
        self.assertEqual(status,400);self.assertEqual(len(self.state.storages),0);self.assertEqual(self.state.submit_count,0)


class PureContract(unittest.TestCase):
    def test_magnet_base32_identity_and_ambiguous_rejection(self):
        a,_=normalize_magnet('magnet:?xt=urn:btih:'+'A'*32);self.assertEqual(a,'0'*40)
        for text in ['https://example.invalid/file','magnet:?xt=urn:btih:abc','magnet:?xt=urn:btih:'+HASH+'&xt=urn:btih:'+('b'*40)]:
            with self.assertRaises(AppError):normalize_magnet(text)

    def test_third_party_cookie_preserves_identity_and_optional_kid(self):
        self.assertEqual(normalize_cookie(COOKIE),COOKIE)
        with self.assertRaises(AppError):normalize_cookie(COOKIE+'\r\nInjected: bad')
        with self.assertRaises(AppError):normalize_cookie(COOKIE+'; UID=502')

    def test_torrent_hashes_exact_info_bytes_and_rejects_bad_structure(self):
        info=b'd4:name4:test6:lengthi1ee';raw=b'd4:info'+info+b'e'
        self.assertIn(hashlib.sha1(info).hexdigest(),torrent_magnet(raw))
        for value in [b'',raw+b'x',b'd4:infoi1ee',b'd4:infod4:name999:xe']:
            with self.assertRaises(AppError):torrent_magnet(value)

    def test_passwords_are_salted_and_not_reversible_plaintext(self):
        a=password_hash('artificial-password-123');b=password_hash('artificial-password-123');self.assertNotEqual(a,b)
        self.assertTrue(password_matches('artificial-password-123',a));self.assertFalse(password_matches('wrong',a))


if __name__=='__main__':unittest.main()
