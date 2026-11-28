import hashlib
import json
import concurrent.futures
import threading
from pathlib import Path

from mikan115.cloud_layout import CloudLayout
from mikan115.errors import AppError
from tests.mock_services import PAYLOAD
from tests.test_workflow import Harness


class SeriesLayout(Harness):
    def setUp(self):
        super().setUp()
        self.work={'title':'动画 第二季','series_title':'动画','mikan_id':'236','tmdb_id':77,'tmdb_season':2,
                   'tmdb_mapping':{'status':'linked','type':'tv','id':77,'season':2},'episodes':[]}
        self.catalog.work=lambda *args,**kwargs:dict(self.work)
        self.catalog.resources=lambda *args,**kwargs:{'bangumi_id':'236','groups':[
            {'subgroupId':'7','subgroupName':'甲组','resources':[
                {'resourceHash':letter*40,'title':f'[甲组] 动画 - {ep:02d} [1080p][简日]',
                 'magnetUrl':'magnet:?xt=urn:btih:'+letter*40} for letter,ep in [('a',1),('b',2)]]
            } ]}
        self.app.sonarr.verify=lambda *args:None
        self.app.sonarr.status=lambda:{'configured':False}
        self.bind()

    def queue(self,letter='a',vps=False):
        status,result,_=self.request('POST','/api/library/resources' if vps else '/api/transfers',
            {'mikan_id':'236','release_ids':[letter*40],'rules':{'resolutions':['1080p']}})
        self.assertEqual(status,202,result)
        return result['tasks'][0]['id']

    def finish(self,tid,episode=1):
        self.app.workflow.tick()
        task=self.store.one('SELECT * FROM transfers WHERE id=?',(tid,))
        self.state.complete(task['alist_task_id'])
        self.state.files[task['task_cid']][0]['name']=f'episode-{episode:02d}.mp4'
        self.app.workflow.poll(tid,True)
        self.app.organizer.tick()
        return task

    def test_resource_vps_action_waits_then_moves_renamed_file_and_downloads_exact_bytes(self):
        tid=self.queue(vps=True)
        waiting=self.app.library.task(self.user,tid)
        self.assertEqual(waiting['status'],'waiting_cloud')
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])
        stage=self.finish(tid)
        self.app.library.tick()
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])
        self.app.layout.tick()
        layout=self.app.layout.row(tid)
        self.assertEqual(layout['status'],'PUBLISHED',layout)
        self.assertEqual(layout['series_name'],'动画')
        self.assertNotIn(stage['task_cid'],self.state.folders)
        self.assertEqual(self.state.folders[layout['series_cid']]['parent'],self.store.binding()['root_cid'])
        self.assertEqual(self.store.binding()['root_name'],'anime')
        file=self.store.one('SELECT * FROM transfer_files WHERE transfer_id=?',(tid,))
        self.assertEqual(file['name'],'动画 - S02E01 [甲组] [简日] [1080p].mp4')
        self.assertTrue(file['path'].endswith('/动画/'+file['name']))
        self.app.library.tick()
        local=self.app.library.task(self.user,tid)
        self.assertEqual(local['status'],'complete',local)
        item=self.store.one('SELECT target_path FROM library_items')
        self.assertEqual(Path(item['target_path']).relative_to(self.app.library.root).parts,('anime','动画',file['name']))
        self.assertEqual(hashlib.sha256(Path(item['target_path']).read_bytes()).digest(),hashlib.sha256(PAYLOAD).digest())
        self.assertEqual(self.state.submit_count,1)
        self.queue(vps=True);self.app.library.tick()
        self.assertEqual(self.state.submit_count,1)
        self.assertEqual(len(self.store.all('SELECT * FROM library_tasks')),1)
        self.assertEqual(self.state.folders['1']['name'],'DailyFiles')

    def test_cloud_only_does_not_download_and_exact_series_id_combines_seasons(self):
        first=self.queue();self.finish(first);self.app.layout.tick();self.app.library.tick()
        self.assertEqual(self.store.all('SELECT * FROM library_requests'),[])
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])
        self.work.update(title='动画 第三季',tmdb_season=3)
        self.work['tmdb_mapping']['season']=3
        second=self.queue('b');self.finish(second,2);self.app.layout.tick()
        a,b=self.app.layout.row(first),self.app.layout.row(second)
        self.assertEqual(a['series_cid'],b['series_cid'])
        names={f['name'] for f in self.state.files[a['series_cid']]}
        self.assertEqual(len(names),2)
        self.assertTrue(any('S03E02' in name for name in names))
        first_local=self.app.library.enqueue(self.user,first)
        second_local=self.app.library.enqueue(self.user,second)
        first_root=self.store.one('SELECT root_path FROM library_tasks WHERE id=?',(first_local['task_id'],))['root_path']
        second_root=self.store.one('SELECT root_path FROM library_tasks WHERE id=?',(second_local['task_id'],))['root_path']
        self.assertEqual(first_root,second_root)
        self.app.library.tick(2)
        self.assertEqual(len(list((self.app.library.root/'anime'/'动画').glob('*.mp4'))),2)

    def test_timeout_after_move_reconciles_same_file_id_after_restart(self):
        tid=self.queue(vps=True);self.finish(tid)
        factory=self.app.workflow.pan_factory;once=[True]
        def wrapped(cookie):
            pan=factory(cookie);move=pan.move_file
            def interrupted(*args):
                move(*args)
                if once[0]:once[0]=False;raise AppError('TEST_TIMEOUT','测试中断',503,uncertain=True)
            pan.move_file=interrupted
            return pan
        self.app.workflow.pan_factory=wrapped
        self.app.layout.tick()
        self.assertEqual(self.app.layout.row(tid)['status'],'WAITING')
        self.assertEqual(self.store.one('SELECT status FROM cloud_outputs')['status'],'PREPARED')
        self.app.layout=CloudLayout(self.store,self.app.workflow,self.catalog);self.app.workflow.layout=self.app.layout
        self.app.layout.tick();self.app.library.tick()
        self.assertEqual(self.app.layout.row(tid)['status'],'PUBLISHED')
        self.assertEqual(self.app.library.task(self.user,tid)['status'],'complete')
        moves=[call for call in self.state.calls if call[1]=='/115-web/files/move']
        self.assertEqual(len(moves),1)

    def test_name_collision_and_foreign_file_never_overwritten_or_downloaded(self):
        tid=self.queue();self.finish(tid)
        task=self.store.one('SELECT * FROM transfers WHERE id=?',(tid,))
        binding,pan=self.app.workflow.scope(task)
        layout=self.app.layout.prepare(task,pan,binding)
        name=self.store.one('SELECT name FROM transfer_files')['name']
        foreign={'name':name,'size':len(PAYLOAD),'fid':'999999','is_dir':False}
        self.state.files[layout['series_cid']]=[foreign]
        self.app.layout.tick()
        self.assertEqual(self.app.layout.row(tid)['status'],'WAITING')
        self.assertEqual(self.state.files[layout['series_cid']],[foreign])
        self.assertTrue(self.state.files[task['task_cid']])
        self.state.files[layout['series_cid']]=[];self.app.layout.tick()
        file=self.store.one('SELECT * FROM transfer_files')
        self.state.files[layout['series_cid']][0]['fid']='999998'
        status,body,_=self.request('GET',f'/api/transfers/{tid}/download/{file["id"]}')
        self.assertEqual(status,409,body)
        self.assertEqual(body['code'],'FILE_SCOPE_CHANGED')

    def test_owned_old_root_migrates_and_unrelated_root_is_preserved(self):
        b=self.store.binding();old='Mikan115-Transfers-'+self.store.instance_id[:8]
        self.state.folders[b['root_cid']]['name']=old
        with self.store.transaction() as db:db.execute('UPDATE binding SET root_name=?',(old,))
        other=self.state.mkdir('0','anime')
        with self.assertRaises(AppError) as error:self.app.workflow.scope()
        self.assertEqual(error.exception.code,'CLOUD_ROOT_CONFLICT')
        self.assertEqual(self.state.folders[b['root_cid']]['name'],old)
        self.state.folders[other]['name']='私人文件'
        self.app.workflow.scope()
        self.assertEqual(self.state.folders[b['root_cid']]['name'],'anime')
        self.assertEqual(self.state.folders[other]['name'],'私人文件')

    def test_directory_creation_timeout_recovers_marker_without_duplicate_series(self):
        tid=self.queue();self.finish(tid)
        factory=self.app.workflow.pan_factory;once=[True]
        def wrapped(cookie):
            pan=factory(cookie);create=pan.create_folder
            def interrupted(parent,name):
                cid=create(parent,name)
                if name.startswith('.anime-') and once[0]:
                    once[0]=False;raise AppError('TEST_TIMEOUT','测试中断',503,uncertain=True)
                return cid
            pan.create_folder=interrupted
            return pan
        self.app.workflow.pan_factory=wrapped
        self.app.layout.tick()
        self.assertIsNone(self.app.layout.row(tid))
        self.app.layout.tick()
        self.assertEqual(self.app.layout.row(tid)['status'],'PUBLISHED')
        self.assertEqual(len(self.store.all('SELECT * FROM cloud_series')),1)
        root=self.store.binding()['root_cid']
        self.assertEqual([f['name'] for f in self.state.folders.values() if f['parent']==root],['动画'])

    def test_same_title_different_confirmed_id_creates_separate_series(self):
        first=self.queue();self.finish(first);self.app.layout.tick()
        self.work.update(tmdb_id=88)
        self.work['tmdb_mapping']['id']=88
        second=self.queue('b');self.finish(second,2);self.app.layout.tick()
        self.assertNotEqual(self.app.layout.row(first)['series_cid'],self.app.layout.row(second)['series_cid'])

    def test_unconfirmed_episode_preserves_original_and_waits_for_review(self):
        tid=self.queue(vps=True);self.app.workflow.tick();task=self.store.one('SELECT * FROM transfers')
        self.state.complete();self.state.files[task['task_cid']][0]['name']='unidentified.mkv'
        with self.store.transaction() as db:
            meta=json.loads(db.execute('SELECT metadata FROM transfer_naming WHERE transfer_id=?',(tid,)).fetchone()['metadata'])
            meta['release']['episodes']=[]
            db.execute('UPDATE transfer_naming SET metadata=? WHERE transfer_id=?',(json.dumps(meta),tid))
        self.app.workflow.poll(tid,True);self.app.organizer.tick();self.app.layout.tick();self.app.library.tick()
        self.assertEqual(self.store.one('SELECT status FROM transfer_naming')['status'],'REVIEW')
        self.assertEqual(self.state.files[task['task_cid']][0]['name'],'unidentified.mkv')
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])
        self.assertEqual(self.app.library.task(self.user,tid)['status'],'waiting_organize')

    def test_episode_link_fetches_exact_parent_season_for_other_release_episodes(self):
        self.work['tmdb_mapping'].update(status='episode_scope',episode=1)
        self.work['episodes']=[{'season_number':2,'episode_number':1}]
        metadata=self.catalog.metadata;calls=[]
        metadata.status=lambda:{'tmdb_configured':True}
        def title(kind,ident,season):
            calls.append((kind,ident,season))
            return {'tmdb_id':77,'defaultSeason':2,'title':'动画','episodes':[{'season_number':2,'episode_number':2}]}
        metadata.title=title
        self.app.sonarr.status=lambda:{'configured':True}
        tid=self.queue('b');self.finish(tid,2);self.app.layout.tick()
        self.assertEqual(calls,[('tv','77',2)])
        self.assertEqual(self.app.layout.row(tid)['status'],'PUBLISHED')
        self.assertIn('S02E02',self.store.one('SELECT name FROM transfer_files')['name'])
        self.assertEqual(self.work['tmdb_mapping']['episode'],1)

    def test_alist_parent_cache_is_refreshed_before_new_series_path_is_used(self):
        self.state.cache_paths=True
        tid=self.queue(vps=True);self.finish(tid);self.app.layout.tick();self.app.library.tick()
        row=self.app.layout.row(tid)
        self.assertEqual(row['status'],'PUBLISHED',row)
        self.assertTrue(row['cleaned'])
        self.assertEqual(self.app.library.task(self.user,tid)['status'],'complete')

    def test_alist_failure_after_publish_recovers_cleanup_and_clears_stale_error(self):
        tid=self.queue();self.finish(tid)
        listing=self.app.workflow.alist.list_files;once=[True]
        def fail_once(path):
            if path.endswith('/动画') and once[0]:
                once[0]=False;raise AppError('ALIST_REJECTED','测试目录缓存暂不可用',503)
            return listing(path)
        self.app.workflow.alist.list_files=fail_once
        self.app.layout.tick()
        self.assertEqual(self.app.layout.row(tid)['status'],'PUBLISHED')
        self.assertFalse(self.app.layout.row(tid)['cleaned'])
        self.assertEqual(self.store.one('SELECT error_code FROM transfers')['error_code'],'ALIST_REJECTED')
        self.app.layout.tick()
        self.assertTrue(self.app.layout.row(tid)['cleaned'])
        self.assertIsNone(self.store.one('SELECT error_code FROM transfers')['error_code'])

    def test_simultaneous_rss_and_manual_vps_dispatch_create_one_local_job(self):
        tid=self.queue();self.finish(tid);self.app.layout.tick()
        episodes=self.app.library._catalog_episodes;barrier=threading.Barrier(2)
        def synchronized(*args):
            result=episodes(*args);barrier.wait(timeout=10);return result
        self.app.library._catalog_episodes=synchronized
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.app.library.enqueue,self.user,tid) for _ in range(2)]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(results[0]['task_id'],results[1]['task_id'])
        self.assertEqual(len(self.store.all('SELECT * FROM library_tasks')),1)
        self.assertEqual(len(self.store.all('SELECT * FROM library_items')),1)

    def test_episode_link_does_not_limit_series_collection_to_first_episode(self):
        self.work['tmdb_mapping'].update(status='episode_scope',episode=1)
        self.work['episodes']=[{'season_number':2,'episode_number':1,'air_date':'2020-01-01'}]
        self.assertEqual(self.app.library._catalog_episodes('236'),(2,[],[]))
        self.store.cache_set('cloud-naming-season:77:2',{'tmdb_id':77,'defaultSeason':2,'episodes':[
            {'season_number':2,'episode_number':n,'air_date':'2020-01-01'} for n in [1,2]]},21600)
        self.assertEqual(self.app.library._catalog_episodes('236'),(2,[1,2],[1,2]))

    def test_shared_local_series_never_adopts_an_untracked_equal_size_file(self):
        tid=self.queue();self.finish(tid);self.app.layout.tick()
        file=self.store.one('SELECT * FROM transfer_files')
        target=self.app.library.root/'anime'/'动画'/file['name'];target.parent.mkdir(parents=True)
        target.write_bytes(b'x'*file['size'])
        with self.assertRaises(AppError) as error:self.app.library.enqueue(self.user,tid)
        self.assertEqual(error.exception.code,'LIBRARY_FILE_CONFLICT')
        self.assertEqual(target.read_bytes(),b'x'*file['size'])
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])
