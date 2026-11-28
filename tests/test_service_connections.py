import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from mikan115.alist import Alist
from mikan115.config import Config
from mikan115.errors import AppError,ProviderError
from mikan115.sonarr import Sonarr
from mikan115.store import Store
from tests.test_workflow import Harness
from tools.configure import configure


class AdminHTTP:
    def __init__(self):
        self.calls=[]
        self.reject=False

    def json(self,method,url,body=None,headers=None):
        self.calls.append((url,body,headers))
        if self.reject:return {'code':401}
        if urlsplit(url).path=='/api/auth/login':
            return {'code':200,'data':{'token':'artificial-session-token'}}
        return {'code':200,'data':['115 Cloud']}


class ServiceConnections(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root)
        self.config=Config(root=self.root,data=self.root,alist_managed=True,
                           alist_url='http://alist:5244',alist_password='generated-admin-password-123',
                           sonarr_managed=True,sonarr_url='http://sonarr:8989',
                           sonarr_api_key='generated-sonarr-key-12345678')
        self.http=AdminHTTP()
        self.alist=Alist(self.config,self.store,self.http)

    def tearDown(self):self.temp.cleanup()

    def test_automatic_alist_gets_and_renews_its_token_without_ck(self):
        self.assertTrue(self.alist.status(check=True)['ready'])
        self.assertTrue(self.alist.status()['managed'])
        logins=[call for call in self.http.calls if '/auth/login' in call[0]]
        self.assertEqual(len(logins),1)
        self.assertEqual(logins[0][1]['password'],self.config.alist_password)
        self.assertEqual(logins[0][2]['Client-Id'],self.store.instance_id)
        self.alist.reset_auth_cache()
        self.alist.preflight()
        self.assertEqual(len([call for call in self.http.calls if '/auth/login' in call[0]]),2)
        self.assertIsNone(self.store.binding(False))
        self.assertNotIn('artificial-session-token',self.store.path.read_bytes().decode('latin1'))

    def test_custom_alist_is_encrypted_and_blank_password_is_retained_only_for_same_server(self):
        result=self.alist.configure({'url':'http://custom:5244','username':'owner','password':'custom-admin-password-123'})
        self.assertTrue(result['ready'])
        self.assertEqual(result['mode'],'custom')
        self.assertNotIn('custom-admin-password-123',json.dumps(result))
        self.assertNotIn('custom-admin-password-123',self.store.path.read_bytes().decode('latin1'))
        self.alist.configure({'url':'http://custom:5244','username':'owner'})
        prior_calls=len(self.http.calls)
        with self.assertRaises(AppError):self.alist.configure({'url':'http://other:5244','username':'owner'})
        self.assertEqual(len(self.http.calls),prior_calls)
        restored=self.alist.configure({'mode':'automatic'})
        self.assertEqual(restored['url'],'http://alist:5244')
        self.assertTrue(restored['managed'])
        self.assertTrue(restored['ready'])

    def test_failed_custom_auth_preserves_existing_connection(self):
        self.alist.preflight()
        self.http.reject=True
        with self.assertRaises(AppError):self.alist.configure({'url':'http://custom:5244','password':'wrong-password'})
        self.assertEqual(self.alist.base_url,self.config.alist_url)
        self.assertIsNone(self.store.setting('alist_connection'))
        self.assertEqual(self.alist.credentials(),('',self.config.alist_password))

    def test_restoring_automatic_ignores_old_manual_token(self):
        self.store.set_setting('alist_credentials',self.store.encrypt(json.dumps({'token':'obsolete-custom-token'})))
        self.assertEqual(self.alist.credentials()[0],'obsolete-custom-token')
        self.alist.configure({'mode':'automatic'})
        self.assertEqual(self.alist.credentials(),('',self.config.alist_password))

    def test_sonarr_custom_detects_version_and_can_restore_generated_credentials(self):
        class HTTP:
            def __init__(self):self.urls=[]
            def json(self,method,url,headers,**kwargs):
                self.urls.append(url)
                if 'custom' in url and '/v3/' in url:raise ProviderError()
                return {'appName':'Sonarr','version':'5.0.0'}
        http=HTTP()
        sonarr=Sonarr(self.store,http,config=self.config)
        self.assertTrue(sonarr.status(check=True)['managed'])
        key='custom-sonarr-key-12345678'
        result=sonarr.configure({'url':'http://custom:8989','api_key':key,'api_version':'auto'})
        self.assertEqual(result['api_version'],'v5')
        self.assertTrue(result['ready'])
        self.assertNotIn(key,self.store.path.read_bytes().decode('latin1'))
        self.assertNotIn(key,json.dumps(result))
        with self.assertRaises(AppError):sonarr.configure({'url':'http://other:8989','api_version':'auto'})
        self.assertFalse(any('other' in url for url in http.urls))
        restored=sonarr.configure({'mode':'automatic'})
        self.assertTrue(restored['managed'])
        self.assertTrue(restored['ready'])
        self.assertEqual(sonarr.config()['api_key'],self.config.sonarr_api_key)

    def test_sonarr_saved_configuration_does_not_claim_healthy_when_service_is_down(self):
        class Offline:
            def json(self,*args,**kwargs):raise ProviderError()
        sonarr=Sonarr(self.store,Offline(),config=self.config)
        result=sonarr.status(check=True)
        self.assertTrue(result['configured'])
        self.assertFalse(result['ready'])
        self.assertEqual(result['code'],'SONARR_UNAVAILABLE')

    def test_deployment_setup_is_repeatable_and_preserves_credentials(self):
        dest=self.root/'deploy'
        dest.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            configure(dest)
            initial=(dest/'.env').read_bytes()
            configure(dest)
        self.assertEqual((dest/'.env').read_bytes(),initial)
        text=initial.decode()
        self.assertIn('ALIST_PASSWORD=',text)
        self.assertNotIn('SONARR_API_KEY=',text)
        self.assertNotIn('SONARR_MANAGED=0',text)


class ServiceHTTPAuthorization(Harness):
    def test_custom_settings_require_admin_and_csrf(self):
        status,_,_=self.request('POST','/api/settings/alist',{'mode':'automatic'},csrf=False)
        self.assertEqual(status,403)
        self.request('POST','/api/admin/users',{'username':'ordinary','password':'ordinary-password-123'})
        self.request('POST','/api/auth/login',{'username':'ordinary','password':'ordinary-password-123'})
        for path in ('/api/settings/alist','/api/settings/sonarr'):
            status,_,_=self.request('GET',path)
            self.assertEqual(status,403)
            status,_,_=self.request('POST',path,{'mode':'automatic'})
            self.assertEqual(status,403)

    def test_active_cloud_transfer_blocks_alist_switch_but_allows_health_check(self):
        self.bind();self.create()
        before=self.store.setting('alist_connection')
        status,result,_=self.request('POST','/api/settings/alist',{'mode':'automatic'})
        self.assertEqual(status,409,result)
        self.assertEqual(self.store.setting('alist_connection'),before)
        status,result,_=self.request('POST','/api/settings/alist',{'check':True})
        self.assertEqual(status,200,result)
        self.assertTrue(result['ready'])
