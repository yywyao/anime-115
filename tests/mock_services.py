import datetime
import hashlib
import http.server
import json
import re
import threading
import urllib.parse
from zoneinfo import ZoneInfo

from mikan115.http_client import HTTP

HASH='a'*40
COOKIE='UID=501_A9_THIRD_PARTY; CID=third-party-cid; SEID=fictional-session; KID=fictional-kid'
PAYLOAD=b'This is an artificial cloud download fixture.\n'+bytes(range(256))*32


class LocalOnlyHTTP(HTTP):
    def open(self,method,url,**kwargs):
        if urllib.parse.urlsplit(url).hostname not in {'127.0.0.1','localhost'}:
            raise AssertionError('Test attempted non-local network access')
        return super().open(method,url,**kwargs)


class State:
    def __init__(self):
        self.lock=threading.RLock();self.folders={'0':{'name':'root','parent':None},'1':{'name':'DailyFiles','parent':'0'}}
        self.files={};self.storages={};self.metas={};self.tasks={};self.cloud={};self.next_cid=100;self.next_storage=1
        self.submit_count=0;self.calls=[];self.cookies=[];self.expired=False;self.malformed_submit=False
        self.fail_submit=False;self.bad_file_name=False;self.feed_hashes=[HASH];self.rss_items=None;self.rename_count=0;self.following_demo=False

    def mkdir(self,parent,name):
        for cid,f in self.folders.items():
            if f['parent']==str(parent) and f['name']==name:return cid
        self.next_cid+=1;cid=str(self.next_cid);self.folders[cid]={'name':name,'parent':str(parent)};return cid

    def path_cid(self,path):
        for storage in self.storages.values():
            mount=storage['mount_path']
            if path==mount or path.startswith(mount+'/'):
                cid=json.loads(storage['addition'])['root_folder_id']
                relative=path[len(mount):].strip('/')
                for part in relative.split('/') if relative else []:
                    matches=[k for k,v in self.folders.items() if v['parent']==cid and v['name']==part]
                    if not matches:return None
                    cid=matches[0]
                return cid
        return None

    def complete(self,tid=None):
        for key,t in self.tasks.items():
            if tid and key!=tid:continue
            t.update(state=2,progress=100)
            remote=self.cloud[t['hash']];remote.update(status=2,percentDone=100,file_id='fixture-file')
            cid=remote['wp_path_id']
            if cid not in self.files:self.files[cid]=[{'name':'show.S02E02.mp4' if self.following_demo else 'episode-01.mp4','size':len(PAYLOAD),'is_dir':False,'fid':cid+'001'}]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    @property
    def state(self):return self.server.state
    def result(self,data,status=200,**headers):
        raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        for k,v in headers.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def html(self,text):
        raw=text.encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.handle_request('GET')
    def do_POST(self):self.handle_request('POST')
    def handle_request(self,method):
        u=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(u.query);path=u.path
        length=int(self.headers.get('Content-Length','0'));raw=self.rfile.read(length) if length else b''
        body={}
        if raw:
            body=json.loads(raw) if self.headers.get_content_type()=='application/json' else {k:v[0] for k,v in urllib.parse.parse_qs(raw.decode()).items()}
        with self.state.lock:
            self.state.calls.append((method,path,q,body))
            if path.startswith('/api/') and path!='/api/auth/login':
                if self.headers.get('Authorization')!='artificial-alist-token':return self.result({'code':401,'message':'unauthorized'})
            if path=='/api/auth/login':
                if body.get('username')=='admin' and body.get('password')=='artificial-alist-password':return self.result({'code':200,'data':{'token':'artificial-alist-token'}})
                return self.result({'code':400,'message':'invalid admin'})
            if path=='/api/admin/driver/names':return self.result({'code':200,'data':['115 Cloud']})
            if path=='/api/admin/storage/list':return self.result({'code':200,'data':{'content':list(self.state.storages.values()),'total':len(self.state.storages)}})
            if path=='/api/admin/storage/create':
                assert body['driver']=='115 Cloud';addition=json.loads(body['addition']);assert addition['cookie']==COOKIE
                assert addition['root_folder_id'] not in {'0','1'};assert addition['qrcode_token']==''
                sid=self.state.next_storage;self.state.next_storage+=1;self.state.storages[sid]={**body,'id':sid,'status':'work'}
                return self.result({'code':200,'data':{'id':sid}})
            if path=='/api/admin/storage/update':
                self.state.storages[int(body['id'])]={**body,'status':'work'};return self.result({'code':200,'data':None})
            if path=='/api/admin/storage/get':return self.result({'code':200,'data':self.state.storages[int(q['id'][0])]})
            if path=='/api/admin/meta/list':return self.result({'code':200,'data':{'content':list(self.state.metas.values()),'total':len(self.state.metas)}})
            if path=='/api/admin/meta/create':
                mid=len(self.state.metas)+1;self.state.metas[mid]={**body,'id':mid};return self.result({'code':200,'data':None})
            if path=='/api/admin/meta/update':
                self.state.metas[int(body['id'])]=body;return self.result({'code':200,'data':None})
            if path=='/api/admin/meta/get':return self.result({'code':200,'data':self.state.metas[int(q['id'][0])]})
            if path=='/api/fs/list':
                cid=self.state.path_cid(body['path'])
                if cid is None:return self.result({'code':500,'message':'not found'})
                items=[{'name':f['name'],'is_dir':True,'size':0} for k,f in self.state.folders.items() if f['parent']==cid]+self.state.files.get(cid,[])
                if self.state.bad_file_name:items=[{'name':'../daily-secret.txt','size':10,'is_dir':False}]
                return self.result({'code':200,'data':{'content':items,'total':len(items)}})
            if path=='/api/fs/add_offline_download':
                assert body['tool']=='115 Cloud' and body['delete_policy']=='delete_never'
                assert len(body['urls'])==1;cid=self.state.path_cid(body['path']);assert cid not in {None,'0','1'}
                ident=urllib.parse.parse_qs(urllib.parse.urlsplit(body['urls'][0]).query)['xt'][0].split(':')[-1]
                self.state.submit_count+=1
                if self.state.fail_submit:return self.result({'code':500,'message':'provider rejected fake CK '+COOKIE})
                tid='alist-job-'+str(self.state.submit_count)
                self.state.tasks[tid]={'id':tid,'state':1,'progress':0,'status':'running','hash':ident}
                self.state.cloud[ident]={'info_hash':ident,'wp_path_id':cid,'status':1,'percentDone':0}
                if self.state.malformed_submit:
                    self.send_response(200);self.send_header('Content-Length','3');self.end_headers();self.wfile.write(b'???');return
                return self.result({'code':200,'data':{'tasks':[self.state.tasks[tid]]}})
            if path=='/api/admin/task/offline_download/info':
                task=self.state.tasks.get(q.get('tid',[''])[0]);return self.result({'code':200,'data':task} if task else {'code':404,'message':'missing'})
            if path=='/api/fs/get':return self.result({'code':200,'data':{'name':'episode-01.mp4','is_dir':False,'sign':'artificial-sign','raw_url':'https://must-not-be-used.invalid/file','header':{'Cookie':COOKIE}}})
            if path.startswith('/p/'):
                assert self.headers.get('Authorization')=='artificial-alist-token';assert q.get('sign')==['artificial-sign']
                content=PAYLOAD;status=200;range_header=self.headers.get('Range');start=0;end=len(content)-1
                if range_header:
                    m=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
                    if m[1]:start=int(m[1]);end=int(m[2]) if m[2] else end
                    else:start=max(0,len(content)-int(m[2]))
                    content=content[start:end+1];status=206
                self.send_response(status);self.send_header('Content-Length',str(len(content)));self.send_header('Content-Type','video/mp4')
                if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{len(PAYLOAD)}')
                self.end_headers();self.wfile.write(content);return
            if path in ('/115-user/','/115-web/files','/115-web/files/add','/115-web/files/batch_rename','/115-offline/lixian/'):
                self.state.cookies.append(self.headers.get('Cookie',''))
                if self.headers.get('Cookie')!=COOKIE or self.state.expired:return self.result({'state':False,'errno':99})
            if path=='/115-user/':return self.result({'state':True,'data':{'user_id':501,'user_name':'Artificial third-party session'}})
            if path=='/115-web/files':
                cid=q['cid'][0];items=[{'cid':k,'n':v['name'],'pid':cid} for k,v in self.state.folders.items() if v['parent']==cid]
                items += [{'fid':v.get('fid',cid+str(i+1).zfill(3)),'cid':cid,'n':v['name'],'s':v['size']} for i,v in enumerate(self.state.files.get(cid,[])) if not v.get('is_dir')]
                return self.result({'state':True,'cid':cid,'count':len(items),'offset':0,'data':items,'path':[{'cid':cid}]})
            if path=='/115-web/files/batch_rename':
                for cid,rows in self.state.files.items():
                    for i,row in enumerate(rows):
                        if str(row.get('fid',cid+str(i+1).zfill(3)))==body.get('fid'):
                            assert cid not in {'0','1'};row['name']=body['file_name'];self.state.rename_count+=1;return self.result({'state':True})
                return self.result({'state':False,'errno':20004})
            if path=='/115-web/files/add':
                cid=self.state.mkdir(body['pid'],body['cname']);return self.result({'state':True,'cid':cid})
            if path=='/115-offline/lixian/':return self.result({'state':True,'page_count':1,'tasks':list(self.state.cloud.values())})
            if path=='/':
                day=(datetime.datetime.now(ZoneInfo('Asia/Tokyo')).weekday()+1)%7
                return self.html(f'<div class="sk-bangumi" data-dayofweek="{day}"><li data-bangumiid="236"><a class="an-text" title="测试动漫" href="/Home/Bangumi/236">测试动漫</a><img data-src="/test-poster.jpg"></li></div>')
            if path=='/Home/Bangumi/236':
                extra='<div class="subgroup-text" id="8"><a class="text-muted">第二字幕组</a><table><tr><td><a class="magnet-link-wrap" href="/Home/Episode/'+('c'*40)+'">[第二字幕组] 测试动漫 - 01 [简繁日][1080p]</a></td></tr></table></div>' if self.state.following_demo else ''
                return self.html('<p class="bangumi-title">测试动漫</p><div class="bangumi-poster" style="background-image: url(/test-poster.jpg)"></div><div class="subgroup-text" id="7"><a class="text-muted">测试字幕组</a><table><tr data-magnet="magnet:?xt=urn:btih:'+HASH+'"><td><a class="magnet-link-wrap" href="/Home/Episode/'+HASH+'">[测试字幕组] 测试动漫 - 01 [简日][1080p]</a></td><td>1 MB</td><td>2026/11/26</td></tr></table></div>'+extra)
            if path=='/Home/Search':return self.html('<a href="/Home/Bangumi/236"><span data-src="/test-poster.jpg"></span><div class="an-text" title="测试动漫"></div></a>')
            if path in ('/RSS/Bangumi','/RSS/MyBangumi'):
                if self.state.rss_items is not None:
                    from xml.sax.saxutils import escape
                    selected=self.state.rss_items
                    if self.state.following_demo and q.get('subgroupid',['0'])[0] in {'7','8'}:
                        group='测试字幕组' if q['subgroupid'][0]=='7' else '第二字幕组';selected=[r for r in selected if r['title'].startswith('['+group+']')]
                    items=''.join('<item><title>'+escape(r['title'])+'</title><guid>'+r.get('guid',r['hash'])+'</guid><enclosure url="magnet:?xt=urn:btih:'+r['hash']+'"/></item>' for r in selected)
                    return self.html('<rss><channel>'+items+'</channel></rss>')
                items=''.join(f'<item><title>RSS episode {i}</title><guid>{h}</guid><enclosure url="magnet:?xt=urn:btih:{h}"/></item>' for i,h in enumerate(self.state.feed_hashes))
                return self.html('<rss><channel>'+items+'</channel></rss>')
            return self.result({'error':'unexpected mock request','path':path},404)


class MockServer(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,state):super().__init__(('127.0.0.1',0),Handler);self.state=state
    @property
    def base(self):return 'http://127.0.0.1:'+str(self.server_port)
    def start(self):self.thread=threading.Thread(target=self.serve_forever,daemon=True);self.thread.start();return self
    def close(self):self.shutdown();self.server_close();self.thread.join(timeout=5)
