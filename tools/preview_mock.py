"""Private UI preview using artificial fixtures. Never talks to real providers."""
import argparse,json,pathlib,sqlite3,sys,tempfile,threading,time
root=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from mikan115.alist import Alist
from mikan115.catalog import Catalog
from mikan115.config import Config
from mikan115.pan115 import Pan115
from mikan115.server import Application,Server,Handler
from mikan115.store import Store
from tests.mock_services import State,MockServer,LocalOnlyHTTP,COOKIE

p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18788);p.add_argument('--test-account',action='store_true');p.add_argument('--fail-config-once',action='store_true');p.add_argument('--following-demo',action='store_true');a=p.parse_args()
with tempfile.TemporaryDirectory(prefix='mikan115-ui-',dir=root.parent) as temp:
    state=State();mock=MockServer(state).start()
    state.following_demo=a.following_demo
    if a.following_demo:state.rss_items=[{'hash':'a'*40,'title':'[测试字幕组] 测试动漫 S02E01 [简日][1080p]'}]
    config=Config(root=root,data=pathlib.Path(temp),mikan_url=mock.base,alist_url=mock.base,alist_password='artificial-alist-password',setup_token='ui-preview-setup-token')
    store=Store(config.data);alist=Alist(config,store,LocalOnlyHTTP(3));catalog=Catalog(store,config,LocalOnlyHTTP(3))
    catalog.seed=pathlib.Path(temp)/'nonexistent-seed.sqlite'
    pan=lambda ck:Pan115(ck,LocalOnlyHTTP(3),my_base=mock.base+'/115-user',web_base=mock.base+'/115-web',offline_base=mock.base+'/115-offline')
    app=Application(config,store=store,alist=alist,pan_factory=pan,catalog=catalog)
    if a.test_account:
        from tests.test_metadata_and_loading import TMDBHTTP
        catalog.metadata.http=TMDBHTTP()
        app.auth.create_user('ui_admin','ui-test-password-2026',admin=True,first=True,setup_token=config.setup_token)
        app.workflow.bind(COOKIE)
        catalog.seed=pathlib.Path(temp)/'fixture-catalog.sqlite'
        db=sqlite3.connect(catalog.seed)
        db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,mikan_id,title,year,season,weekday,payload)')
        fixture={'id':'catalog~fixture','canonical_id':'catalog~fixture','mikan_id':'236','bangumiId':'236','title':'验收作品 第二季','year':2026,'defaultSeason':2,'display_season':2,'air_weekday':4,
                 'poster_url':'https://mikanani.me/images/Bangumi/202610/c2a75f6a.jpg?width=400&format=webp',
                 'image':'https://mikanani.me/images/Bangumi/202610/c2a75f6a.jpg?width=400&format=webp',
                 'backdrop_url':'https://image.tmdb.org/t/p/original/ht6WUmez8cFbwlrFmGdjbYyn3RG.jpg',
                 'ratings':{'tmdb':8.6,'bangumi':7.4,'mal':8.42},'provider_ids':{'mal':'64534','bangumi':'665390'},'tmdb_id':77,'tmdb_season':2,'tmdb_mapping':{'status':'linked'},
                 'episodes':[{'id':1,'season_number':2,'episode_number':1,'title':'验收首集','air_date':'2026-10-01','overview':'测试剧集资料。'}]}
        for season in ['秋','夏']:
            value={**fixture,'id':'catalog~fixture'+season,'canonical_id':'catalog~fixture'+season}
            db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',(value['id'],'236',value['title'],2026,season,4,json.dumps(value,ensure_ascii=False)))
        db.commit();db.close()
        def finish_tasks():
            while not app.stop_event.wait(.5):
                with state.lock:
                    if a.following_demo and store.one('SELECT count(*) n FROM subscriptions WHERE removed=0')['n']>0:
                        state.rss_items=[{'hash':'b'*40,'title':'[测试字幕组] 测试动漫 S02E02 [简日][1080p]'}, {'hash':'c'*40,'title':'[第二字幕组] 测试动漫 S02E02 [简中][1080p]'}, {'hash':'d'*40,'title':'[测试字幕组] 测试动漫 S02E02 [繁日][720p]'}]
                    state.complete()
        threading.Thread(target=finish_tasks,daemon=True).start()
    class PreviewHandler(Handler):
        config_failed=False
        def do_GET(self):
            if a.fail_config_once and self.path.split('?',1)[0]=='/api/auth/config' and not type(self).config_failed:
                type(self).config_failed=True
                raw=b'artificial-invalid-json-response'
                self.send_response(503);self.headers_common();self.send_header('Content-Type','text/plain')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            return super().do_GET()
    server=Server(('127.0.0.1',a.port),app);server.RequestHandlerClass=PreviewHandler;app.start_worker()
    print('ISOLATED_MOCK_PREVIEW_READY',a.port,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();app.close();mock.close()
