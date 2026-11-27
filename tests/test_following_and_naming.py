import contextlib,hashlib,json,pathlib,sqlite3,tempfile,threading,time,unittest
from mikan115 import releases
from mikan115.errors import AppError,ProviderError
from mikan115.sonarr import Sonarr
from mikan115.store import Store
from tests.test_workflow import Harness
from tests.mock_services import HASH,PAYLOAD

class Labels(unittest.TestCase):
    def test_chinese_and_english_subtitle_combinations(self):
        for text,version in [('简中','chs'),('繁體中文','cht'),('中日双语','zh-ja'),('简日','chs-ja'),('繁日','cht-ja'),('简繁','chs-cht'),('简繁日','chs-cht-ja'),('CHS_CHT_JP','chs-cht-ja'),('GB_JP','chs-ja'),('简／繁','chs-cht'),('简&繁&日','chs-cht-ja'),('无字幕','none')]:
            with self.subTest(text=text):self.assertEqual(releases.parse('[组] 动画 - 03 [1080p]['+text+']')['version'],version)
        self.assertEqual(releases.parse('Animation [1080p]')['version'],'unknown')
    def test_episode_ranges_revision_and_special_season(self):
        self.assertEqual(releases.parse('Show S00E02')['season'],0)
        self.assertEqual(releases.parse('[组] 动画 - 03v2 [简中]')['episodes'],[3])
        self.assertEqual(releases.parse('[组] 动画 - 03v2 [简中]')['revision'],2)
        self.assertEqual(releases.parse('[组] 动画 [01-12] [1080p]')['episodes'],list(range(1,13)))
        self.assertEqual(releases.parse('Show [1080p] [2026]')['episodes'],[])
    def test_keyword_rules_are_literal_and_batches_require_opt_in(self):
        item={'title':'[组] 动画 [01-12] [1080p][简中]','release':releases.parse('[组] 动画 [01-12] [1080p][简中]')}
        rule=releases.rules();self.assertEqual(releases.admit(item,rule),'合集未启用')
        self.assertEqual(releases.admit(item,releases.rules({'allow_batches':True,'exclude':['.*']})),'')
        self.assertEqual(releases.admit(item,releases.rules({'allow_batches':True,'exclude':['动画']})),'命中排除词')
    def test_invalid_rules_and_impossible_range_rejected(self):
        for value in [{'season':-1},{'wait_minutes':True},{'group_ids':['evil']},{'versions':['guess']},{'dedupe':'title'},{'min_episode':10,'max_episode':2}]:
            with self.subTest(value=value),self.assertRaises(AppError):releases.rules(value)
    def test_absolute_offset_matches_explicit_season_episode_without_double_offset(self):
        rule=releases.rules({'season':2,'episode_offset':-13});absolute={'key':'a','bangumi_id':'4098','identity_season':2,'release':releases.parse('[组] Ice - 22 [简日]')}
        relative={'key':'b','bangumi_id':'4098','release':releases.parse('Ice S02E09 [简日]')}
        self.assertEqual(releases.episode_keys(absolute,rule),releases.episode_keys(relative,rule))
        self.assertEqual(releases.normalized_episodes(relative['release'],rule),[9])
        self.assertEqual(releases.admit({**absolute,'title':'Show - 22'},releases.rules({**rule,'min_episode':9,'max_episode':9})), '')
    def test_different_works_and_subtitle_variants_have_distinct_scopes(self):
        a={'key':'a','bangumi_id':'10','release':releases.parse('Show - 01 [简日]')};b={**a,'bangumi_id':'20'}
        self.assertNotEqual(releases.episode_keys(a,releases.rules()),releases.episode_keys(b,releases.rules()))
        self.assertEqual(releases.episode_keys(a,releases.rules({'season':1})),releases.episode_keys(a,releases.rules({'season':2})))
        c={**a,'release':releases.parse('Show - 01 [简中]')}
        self.assertNotEqual(releases.episode_keys(a,releases.rules({'dedupe':'episode_version'})),releases.episode_keys(c,releases.rules({'dedupe':'episode_version'})))
    def test_old_subscription_schema_is_migrated_without_deleting_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/'mikan115.sqlite'
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE subscriptions (id TEXT PRIMARY KEY,user_id INTEGER,work_id TEXT,bangumi_id TEXT,subgroup_id TEXT,title TEXT,enabled INTEGER DEFAULT 1,initialized INTEGER DEFAULT 0,last_polled REAL DEFAULT 0,last_error TEXT,UNIQUE(user_id,bangumi_id,subgroup_id))')
                db.execute("INSERT INTO subscriptions(id,user_id,work_id,bangumi_id,subgroup_id,title) VALUES('old',1,'236','236','7','old follow')")
                db.commit()
            store=Store(pathlib.Path(folder));row=store.one("SELECT * FROM subscriptions WHERE id='old'")
            self.assertEqual(row['title'],'old follow');self.assertEqual(row['rules_json'],'{}');self.assertEqual(row['revision'],1)

class Following(Harness):
    def test_explicit_rss_group_is_not_overwritten_by_ambiguous_hash_cache(self):
        with self.store.transaction() as db:
            for group in ['357','370']:db.execute('INSERT INTO release_sources VALUES(?,?,?,?)',(HASH,'236',group,time.time()))
        item={'key':'key','bangumi_id':'236','resource_hash':HASH,'release':releases.parse('动画 - 01','357','SweetSub')}
        self.assertEqual(self.app.rss.identify(item)['release']['group_id'],'357')
        self.assertIsNone(self.app.rss.cached_source(HASH,'236'))
    def test_publisher_id_is_translated_to_rss_group_id_by_explicit_page_link(self):
        page='<p class="bangumi-title">测试动漫</p><div class="subgroup-text" id="357"><a href="/Home/PublishGroup/212">SweetSub</a><a href="/RSS/Bangumi?bangumiId=236&amp;subgroupid=357" class="mikan-rss"></a></div>'
        def fetch(path,*args,**kwargs):
            if path.startswith('/Home/Episode/'):return '<a href="/Home/Bangumi/236">work</a><a href="/Home/PublishGroup/212">group</a>'
            if path=='/Home/Bangumi/236':return page
            raise AssertionError(path)
        self.catalog.fetch=fetch
        item={'key':'key','bangumi_id':'','episode_path':'/Home/Episode/'+HASH,'release':releases.parse('动画 - 01')}
        identified=self.app.rss.identify(item);self.assertEqual(identified['release']['group_id'],'357');self.assertEqual(identified['bangumi_id'],'236')
        self.assertEqual(identified['release']['group'],'SweetSub')
    def test_unassigned_group_label_is_not_the_subscription_button(self):
        page='<p class="bangumi-title">测试动漫</p><div class="subgroup-text" id="202">&#x751F;&#x8089;/&#x4E0D;&#x660E;&#x5B57;&#x5E55;<a href="/RSS/Bangumi?bangumiId=236&amp;subgroupid=202" class="mikan-rss"></a><a>订阅</a></div>'
        self.catalog.fetch=lambda *a,**k:page
        group=self.catalog.mikan_detail('236')['subgroups'][0];self.assertEqual(group['subgroupName'],'生肉/不明字幕');self.assertEqual(group['subgroupId'],'202')
    def test_known_group_is_resolved_without_an_extra_resource_page_request(self):
        self.catalog.mikan_detail('236')
        item={'key':'key','bangumi_id':'236','episode_path':'/Home/Episode/'+HASH,'release':releases.parse('[测试字幕组] 测试动漫 - 01')}
        def reject(*args,**kwargs):raise AssertionError('Unnecessary upstream request')
        self.catalog.fetch=reject
        self.assertEqual(self.app.rss.identify(item)['release']['group_id'],'7')
    def sub(self,rule=None,sg='0'):
        self.state.rss_items=[]
        result=self.app.rss.create(self.user,{'bangumi_id':'236','subgroup_id':sg,'rules':rule or {}})
        return self.store.one('SELECT * FROM subscriptions WHERE id=?',(result['id'],))
    def item(self,letter,episode=1,version='简日',group='测试字幕组',resolution='1080p'):
        return {'hash':letter*40,'title':f'[{group}] 测试动漫 - {episode:02d} [{version}][{resolution}]'}
    def test_create_before_connection_and_preview_never_submit(self):
        sub=self.sub();self.state.rss_items=[self.item('b')]
        result=self.app.rss.preview(self.user,{},sub['id']);self.assertTrue(result['dry_run']);self.assertTrue(result['items'][0]['admitted'])
        self.assertEqual(self.state.submit_count,0);self.assertEqual(self.store.all('SELECT * FROM subscription_claims'),[])
        self.assertFalse(self.app.rss.settings()['connection_ready'])
    def test_priority_selects_one_group_and_preserves_rejection_reasons(self):
        self.bind();sub=self.sub({'group_ids':['8','7'],'versions':['chs-ja']})
        with self.store.transaction() as db:db.execute('INSERT INTO work_groups VALUES(?,?,?,?)',('236','8','第二字幕组',time.time()))
        self.state.rss_items=[self.item('b'),self.item('c',group='第二字幕组'),self.item('d',version='繁中')]
        self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
        task=self.store.one('SELECT * FROM transfers');self.assertEqual(task['infohash'],'c'*40)
        records=self.app.rss.history(self.user,sub['id'])['items'];self.assertIn('字幕版本不符',[r['reason'] for r in records]);self.assertIn('DUPLICATE',[r['status'] for r in records])
    def test_preferred_later_candidate_shares_original_wait_window(self):
        self.bind();sub=self.sub({'group_ids':['8','7'],'wait_minutes':5})
        with self.store.transaction() as db:db.execute('INSERT INTO work_groups VALUES(?,?,?,?)',('236','8','第二字幕组',time.time()))
        self.state.rss_items=[self.item('b')];self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,0)
        with self.store.transaction() as db:db.execute('UPDATE subscription_releases SET first_seen=?',(time.time()-301,))
        self.state.rss_items.append(self.item('c',group='第二字幕组'));self.app.rss.poll(sub)
        self.assertEqual(self.state.submit_count,1);self.assertEqual(self.store.one('SELECT infohash FROM transfers')['infohash'],'c'*40)
    def test_cross_subscription_episode_dedup_and_restart(self):
        self.bind();a=self.sub();b=self.sub(sg='7');self.state.rss_items=[self.item('b')]
        self.app.rss.poll(a);self.assertEqual(self.state.submit_count,1)
        from mikan115.following import RSS
        self.app.rss=RSS(self.store,self.config,self.catalog,self.app.workflow)
        self.state.rss_items=[self.item('c')];self.app.rss.poll(b);self.assertEqual(self.state.submit_count,1)
    def test_same_guid_changed_hash_is_new_but_same_hash_changed_guid_is_not(self):
        self.bind();sub=self.sub();item=self.item('b');item['guid']='first';self.state.rss_items=[item];self.app.rss.poll(sub)
        self.state.rss_items=[{**item,'guid':'changed'}];self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
    def test_all_groups_accepts_new_group_and_version_dedup_keeps_both(self):
        self.bind();sub=self.sub({'dedupe':'episode_version'});self.state.rss_items=[self.item('b',group='新字幕组'),self.item('c',version='繁日',group='新字幕组')]
        self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,2)
    def test_pause_remove_and_rule_revision_stop_enqueue(self):
        self.bind();sub=self.sub();self.state.rss_items=[self.item('b')]
        original=self.app.workflow.create
        def changed(*args,**kwargs):
            self.app.rss.update(self.user,sub['id'],{'rules':{'exclude':['测试']}})
            return original(*args,**kwargs)
        self.app.workflow.create=changed;self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,0)
        self.app.rss.configure({'paused':True});self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,0)
        self.app.rss.update(self.user,sub['id'],{'remove':True});self.assertEqual(self.app.rss.list(self.user),[])
    def test_editing_rule_reconsiders_filtered_items(self):
        self.bind();sub=self.sub({'versions':['cht']});self.state.rss_items=[self.item('b')];self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,0)
        self.app.rss.update(self.user,sub['id'],{'rules':{'versions':['chs-ja']}});self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
    def test_untrusted_feed_urls_and_xml_entities_are_rejected(self):
        for url in ['http://169.254.169.254/RSS/Bangumi?bangumiId=236','https://mikanani.me@evil.test/RSS/Bangumi?bangumiId=236','https://mikanani.me/RSS/Bangumi?bangumiId=236&url=http://evil','https://mikanani.me/RSS/Bangumi?bangumiId=1&bangumiId=2']:
            with self.subTest(url=url),self.assertRaises(AppError):self.app.rss.source({'rss_url':url})
        self.catalog.rss_bytes=lambda *a:b'<!DOCTYPE rss [<!ENTITY a "bad">]><rss/>'
        with self.assertRaises(AppError):self.app.rss.entries('236','0')
    def test_personal_feed_token_encrypted_and_not_returned(self):
        token='private-rss-token-1234567890';self.state.rss_items=[]
        self.app.rss.create(self.user,{'rss_url':self.mock.base+'/RSS/MyBangumi?token='+token})
        self.assertNotIn(token,self.store.path.read_bytes().decode('latin1'));self.assertNotIn(token,json.dumps(self.app.rss.list(self.user)))
    def test_http_ownership_admin_scope_and_csrf(self):
        sub=self.sub();self.request('POST','/api/admin/users',{'username':'ordinary','password':'ordinary-password-123'})
        self.request('POST','/api/auth/login',{'username':'ordinary','password':'ordinary-password-123'})
        status,data,_=self.request('GET','/api/subscriptions?scope=all');self.assertEqual(data['subscriptions'],[])
        for suffix in ['', '/preview','/check']:
            status,_,_=self.request('POST','/api/subscriptions/'+sub['id']+suffix,{});self.assertEqual(status,404)
        status,_,_=self.request('POST','/api/settings/following',{'paused':True});self.assertEqual(status,403)
        status,_,_=self.request('POST','/api/subscriptions',{'bangumi_id':'236'},csrf=False);self.assertEqual(status,403)

class Naming(Harness):
    def ready(self):
        self.bind();self.catalog.save_works([{'id':'test-work','mikan_id':'236','title':'测试动漫','tmdb_season':2,'tmdb_mapping':{'status':'linked'},'provider_ids':{'tvdb':'123'}}])
        job=self.app.workflow.create(self.user,'magnet:?xt=urn:btih:'+HASH,'[测试字幕组] 测试动漫 - 01 [简日][1080p]','test-work')
        self.state.complete();self.app.workflow.poll(job['id']);return self.app.workflow.get(self.user,job['id'])
    def test_preview_then_rename_keeps_scope_sidecars_and_download_bytes(self):
        job=self.ready();task=self.store.one('SELECT * FROM transfers WHERE id=?',(job['id'],))
        self.state.files[task['task_cid']].append({'name':'episode-01.chs.ass','size':12,'fid':'90001'})
        with self.store.transaction() as db:db.execute('INSERT INTO transfer_files(id,transfer_id,path,name,size) VALUES(?,?,?,?,?)',('s'*32,job['id'],task['task_path']+'/episode-01.chs.ass','episode-01.chs.ass',12))
        plan=self.app.organizer.preview(self.user,job['id']);self.assertEqual(len(plan['files']),2);self.assertEqual(self.state.rename_count,0)
        self.assertIn('S02E01',plan['files'][0]['new_name']);self.assertIn('简日',plan['files'][0]['new_name'])
        result=self.app.organizer.apply(self.user,job['id'],plan['digest']);self.assertEqual(result['renamed'],2)
        file=next(f for f in result['task']['files'] if f['name'].endswith('.mp4'))
        status,raw,_=self.request('GET',file['download_url'],binary=True);self.assertEqual(status,200);self.assertEqual(hashlib.sha256(raw).digest(),hashlib.sha256(PAYLOAD).digest())
        self.assertTrue(all(m['password'] for m in self.state.metas.values()));self.assertEqual(self.state.rename_count,2)
    def test_collision_and_tampered_path_do_not_rename(self):
        job=self.ready();plan=self.app.organizer.preview(self.user,job['id']);task=self.store.one('SELECT * FROM transfers WHERE id=?',(job['id'],))
        target=plan['files'][0]['new_name'];self.state.files[task['task_cid']].append({'name':target,'size':1,'fid':'90002'})
        with self.assertRaises(AppError):self.app.organizer.apply(self.user,job['id'],plan['digest'])
        self.assertEqual(self.state.rename_count,0)
        with self.store.transaction() as db:db.execute('UPDATE transfer_files SET path=? WHERE transfer_id=?',('/daily/episode-01.mp4',job['id']))
        with self.assertRaises(AppError):self.app.organizer.apply(self.user,job['id'],plan['digest'])
        self.assertEqual(self.state.rename_count,0)
    def test_ambiguous_response_is_reconciled_without_second_rename(self):
        job=self.ready();plan=self.app.organizer.preview(self.user,job['id']);original=self.pan;once=[False]
        def factory(cookie):
            pan=original(cookie);rename=pan.rename_file
            def ambiguous(*args):
                rename(*args)
                if not once[0]:once[0]=True;raise ProviderError(uncertain=True)
            pan.rename_file=ambiguous;return pan
        self.app.workflow.pan_factory=factory
        with self.assertRaises(AppError):self.app.organizer.apply(self.user,job['id'],plan['digest'])
        result=self.app.organizer.apply(self.user,job['id'],plan['digest']);self.assertEqual(self.state.rename_count,1);self.assertIn('S02E01',result['task']['files'][0]['name'])
    def test_automatic_rename_unknown_episode_keeps_original_and_reports_reason(self):
        job=self.ready();task=self.store.one('SELECT * FROM transfers WHERE id=?',(job['id'],))
        self.state.files[task['task_cid']][0]['name']='unknown.mkv'
        with self.store.transaction() as db:
            db.execute('UPDATE transfer_files SET path=?,name=? WHERE transfer_id=?',(task['task_path']+'/unknown.mkv','unknown.mkv',job['id']))
            db.execute('INSERT INTO transfer_naming VALUES(?,?,?,?,?,?)',(job['id'],json.dumps({'release':releases.parse('unknown'),'rules':{},'work_id':'test-work'}),1,'PENDING',None,time.time()))
        self.app.organizer.tick();self.assertEqual(self.state.rename_count,0);self.assertEqual(self.store.one('SELECT status FROM transfer_naming')['status'],'REVIEW')
    def test_invalid_digest_and_non_owner_cannot_rename(self):
        job=self.ready()
        with self.assertRaises(AppError):self.app.organizer.apply(self.user,job['id'],'invalid')
        with self.assertRaises(AppError):self.app.organizer.preview({'id':999,'is_admin':False},job['id'])
        self.assertEqual(self.state.rename_count,0)

    def test_resource_file_episode_mismatch_is_kept_unchanged(self):
        job=self.ready();task=self.store.one('SELECT * FROM transfers WHERE id=?',(job['id'],))
        with self.store.transaction() as db:db.execute('UPDATE transfers SET title=? WHERE id=?',('[字幕组] 测试动漫 - 02 [简日]',job['id']))
        plan=self.app.organizer.preview(self.user,job['id']);self.assertEqual(plan['files'],[])
        self.assertIn('集号不一致',plan['skipped'][0]['reason']);self.assertEqual(self.state.rename_count,0)
    def test_absolute_release_and_relative_file_get_one_correct_episode_number(self):
        job=self.ready();task=self.store.one('SELECT * FROM transfers WHERE id=?',(job['id'],));row=self.state.files[task['task_cid']][0];row['name']='Show.S02E09.mp4'
        meta={'work_id':'test-work','release':releases.parse('[组] Show - 22 [简日]'),'rules':{'season':2,'episode_offset':-13}}
        with self.store.transaction() as db:
            db.execute('UPDATE transfer_files SET path=?,name=? WHERE transfer_id=?',(task['task_path']+'/'+row['name'],row['name'],job['id']))
            db.execute('INSERT INTO transfer_naming VALUES(?,?,?,?,?,?)',(job['id'],json.dumps(meta),0,'PENDING',None,time.time()))
        plan=self.app.organizer.preview(self.user,job['id']);self.assertEqual(len(plan['files']),1);self.assertIn('S02E09',plan['files'][0]['new_name'])

class SonarrContract(unittest.TestCase):
    def test_configuration_encrypted_and_exact_tvdb_episode_required(self):
        class Mock:
            tvdb=123
            def json(self,method,url,headers):
                self.headers=headers
                if '/system/status' in url:return {'appName':'Sonarr','version':'4.0.0'}
                return {'series':{'tvdbId':self.tvdb,'id':7},'parsedEpisodeInfo':{'seasonNumber':2,'episodeNumbers':[3]}}
        with tempfile.TemporaryDirectory() as folder:
            store=Store(pathlib.Path(folder));http=Mock();sonarr=Sonarr(store,http);key='artificial-sonarr-key-123456'
            result=sonarr.configure({'url':'http://sonarr:8989','api_key':key});self.assertNotIn(key,json.dumps(result));self.assertNotIn(key,store.path.read_bytes().decode('latin1'))
            work={'provider_ids':{'tvdb':'123'}};self.assertEqual(sonarr.verify('Show S02E03',work,2,3)['source'],'Sonarr')
            http.tvdb=999
            with self.assertRaises(AppError):sonarr.verify('Show S02E03',work,2,3)
            self.assertEqual(http.headers['X-Api-Key'],key)
