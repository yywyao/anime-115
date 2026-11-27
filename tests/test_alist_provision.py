import json
from types import SimpleNamespace
import unittest
import urllib.parse

from mikan115.alist import Alist
from mikan115.errors import AppError, ProviderError


COOKIE='UID=501; CID=cid-1; SEID=session-1'


class Store:
    def __init__(self):
        self.values={}

    def setting(self,key):
        return self.values.get(key,'')

    def set_setting(self,key,value):
        self.values[key]=value

    def encrypt(self,value):
        return 'enc:'+value

    def decrypt(self,value):
        return value[4:] if value.startswith('enc:') else value


class AListHTTP:
    def __init__(self,*,drivers=None,empty_create=False):
        self.drivers=['115 Cloud'] if drivers is None else drivers
        self.empty_create=empty_create
        self.storages=[]
        self.metas=[]
        self.calls=[]
        self.next_id=1
        self.statuses=[]

    def json(self,method,url,body=None,headers=None):
        path=urllib.parse.urlsplit(url).path
        self.calls.append((method,path,body or {}))
        if path=='/api/auth/login':return {'code':200,'data':{'token':'admin-token'}}
        if path=='/api/admin/driver/names':return {'code':200,'data':self.drivers}
        if path=='/api/admin/storage/list':return {'code':200,'data':{'content':list(self.storages),'total':len(self.storages)}}
        if path=='/api/admin/storage/get':
            sid=int(url.split('id=')[1]);row=next(s for s in self.storages if int(s['id'])==sid)
            if self.statuses:row['status']=self.statuses.pop(0)
            return {'code':200,'data':row}
        if path=='/api/admin/storage/create':
            row=dict(body);row['id']=self.next_id;self.next_id+=1;row['status']='init';self.storages.append(row)
            return {'code':200,'data':{} if self.empty_create else {'id':row['id']}}
        if path=='/api/admin/storage/update':
            sid=int(body['id']);row=next(s for s in self.storages if int(s['id'])==sid);row.update(body);row['status']='work'
            return {'code':200,'data':None}
        if path=='/api/admin/meta/list':return {'code':200,'data':{'content':list(self.metas),'total':len(self.metas)}}
        if path=='/api/admin/meta/create':
            row=dict(body);row['id']=len(self.metas)+1;self.metas.append(row);return {'code':200,'data':None}
        if path=='/api/admin/meta/update':
            sid=int(body['id']);row=next(s for s in self.metas if int(s['id'])==sid);row.update(body);return {'code':200,'data':None}
        if path=='/api/admin/meta/get':
            sid=int(url.split('id=')[1]);return {'code':200,'data':next(s for s in self.metas if int(s['id'])==sid)}
        raise AssertionError(path)


def make_alist(http):
    config=SimpleNamespace(alist_url='http://alist.test',alist_username='admin',
                           alist_password='admin-password',alist_token='')
    return Alist(config,Store(),http)


class AlistProvisionTests(unittest.TestCase):
    def test_preflight_requires_official_115_cloud_driver(self):
        alist=make_alist(AListHTTP(drivers=['115 Open']))
        with self.assertRaises(ProviderError) as error:alist.preflight()
        self.assertEqual(error.exception.code,'ALIST_115_DRIVER_UNAVAILABLE')

    def test_create_without_id_rereads_owned_mount_and_waits_for_work(self):
        http=AListHTTP(empty_create=True);http.statuses=['init','work']
        alist=make_alist(http)
        sid=alist.provision(COOKIE,'501','77','/mikan115-demo','marker-501')
        self.assertEqual(sid,1)
        paths=[call[1] for call in http.calls]
        self.assertLess(paths.index('/api/admin/meta/create'),paths.index('/api/admin/storage/create'))
        self.assertEqual(http.storages[0]['status'],'work')

    def test_unrelated_mount_is_never_taken_over(self):
        http=AListHTTP();http.storages=[{'id':9,'mount_path':'/mikan115-demo','remark':'other','driver':'115 Cloud'}]
        alist=make_alist(http)
        with self.assertRaises(ProviderError) as error:
            alist.provision(COOKIE,'501','77','/mikan115-demo','marker-501')
        self.assertEqual(error.exception.code,'ALIST_MOUNT_CONFLICT')
        self.assertFalse(any(path.endswith('/storage/update') or path.endswith('/storage/create') for _,path,_ in http.calls))

    def test_reserved_mount_reports_missing_managed_and_tampered(self):
        http=AListHTTP();alist=make_alist(http)
        self.assertEqual(alist.inspect_reserved_mount('/mikan115-demo','marker')['state'],'missing')
        http.storages=[{'id':1,'mount_path':'/mikan115-demo','remark':'marker','driver':'115 Cloud'}]
        self.assertEqual(alist.inspect_reserved_mount('/mikan115-demo','marker')['state'],'managed')
        self.assertEqual(alist.inspect_reserved_mount('/mikan115-demo','other')['state'],'tampered')

    def test_optional_kid_added_by_alist_is_allowed_but_required_fields_must_match(self):
        http=AListHTTP();http.statuses=['work'];alist=make_alist(http)
        alist.provision(COOKIE,'501','77','/mikan115-demo','marker-501')
        row=http.storages[0];addition=json.loads(row['addition']);addition['cookie']=COOKIE+'; KID=driver-added';row['addition']=json.dumps(addition);row['status']='work'
        self.assertEqual(alist.assert_scope(1,'77','/mikan115-demo','marker-501','501',COOKIE)['id'],1)
        with self.assertRaises(ProviderError) as error:
            alist.assert_scope(1,'77','/mikan115-demo','marker-501','501','UID=501; CID=other; SEID=session-1')
        self.assertEqual(error.exception.code,'ALIST_COOKIE_MISMATCH')


if __name__=='__main__':unittest.main()
