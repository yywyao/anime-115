from pathlib import Path
from mikan115 import releases
from mikan115.errors import AppError
from tests.mock_services import PAYLOAD
from tests.test_workflow import Harness


class InventoryActions(Harness):
    def setUp(self):
        super().setUp();self.bind()
        self.work={'id':'work-236','mikan_id':'236','title':'动画','series_title':'动画','tmdb_id':77,'tmdb_season':1,
                   'tmdb_mapping':{'status':'linked','type':'tv','id':77,'season':1},
                   'episodes':[{'season_number':1,'episode_number':n,'air_date':'2020-01-01'} for n in range(1,43)]}
        self.catalog.work=lambda *args,**kwargs:dict(self.work)
        def row(letter,ep):return {'resourceHash':letter*40,'title':f'[FSD] 动画 - {ep:02d} [1080p][简日]',
                                  'magnetUrl':'magnet:?xt=urn:btih:'+letter*40}
        self.catalog.resources=lambda *args,**kwargs:{'bangumi_id':'236','groups':[
            {'subgroupId':'7','subgroupName':'FSD','resources':[row('b',41),row('c',42)]}]}
        self.app.sonarr.verify=lambda *args:None;self.app.sonarr.status=lambda:{'configured':False}

    def own42(self):
        tid=self.app.workflow.create(self.user,'magnet:?xt=urn:btih:'+'a'*40,'[FSD] 动画 - 42 [1080p][简日]','236',
            naming={'work_id':'236','bangumi_id':'236','release':releases.parse('[FSD] 动画 - 42 [1080p][简日]','7','FSD'),
                    'rules':releases.rules({'season':1})},enqueue_only=True)['id']
        self.app.workflow.tick();task=self.store.one('SELECT * FROM transfers WHERE id=?',(tid,));self.state.complete()
        self.state.files[task['task_cid']][0]['name']='file.S01E42.mp4'
        self.app.workflow.poll(tid,True);self.app.organizer.tick();self.app.layout.tick()
        return tid

    def body(self,destination='cloud',action='missing'):
        return {'mikan_id':'236','action':action,'destination':destination,'rules':{'resolutions':['1080p']}}

    def test_cloud_missing_hides_owned_episode42_without_any_vps_download(self):
        self.own42();plan=self.app.batch.action(self.user,self.body())
        self.assertEqual(plan['owned_episodes'],[42])
        self.assertNotIn(42,plan['missing_episodes'])
        self.assertEqual(plan['groups'][0]['episode_count'],1)
        self.assertEqual([r['episodes'] for r in plan['groups'][0]['resources']],[[41]])
        self.assertEqual([r['episodes'] for r in plan['items']],[[41]])
        self.assertEqual(self.store.all('SELECT * FROM library_tasks'),[])

    def test_first_collection_missing_can_find_releases_before_local_library_exists(self):
        plan=self.app.batch.action(self.user,self.body())
        self.assertEqual(plan['owned_episodes'],[])
        self.assertEqual(plan['selected_episodes'],[41,42])
        self.assertEqual(len(plan['missing_episodes']),42)

    def test_missing_and_complete_have_distinct_airdate_behavior_and_skip_pending(self):
        self.work['episodes'].append({'season_number':1,'episode_number':43,'air_date':'2099-01-01'})
        resources=self.catalog.resources('236')['groups'][0]['resources']
        resources.append({'resourceHash':'d'*40,'title':'动画 - 43 [1080p][简日]','magnetUrl':'magnet:?xt=urn:btih:'+'d'*40})
        self.catalog.resources=lambda *args,**kwargs:{'groups':[{'subgroupId':'7','subgroupName':'FSD','resources':resources}]}
        self.assertEqual(self.app.batch.action(self.user,self.body())['selected_episodes'],[41,42])
        self.assertEqual(self.app.batch.action(self.user,self.body(action='complete'))['selected_episodes'],[41,42,43])
        body={**self.body(),'release_ids':['b'*40]};plan=self.app.batch.action(self.user,body)
        self.app.batch.action(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(self.app.batch.action(self.user,self.body())['selected_episodes'],[42])

    def test_same_hash_cannot_rebind_a_different_confirmed_work(self):
        self.own42();other={**self.work,'id':'other','tmdb_id':88,'tmdb_mapping':{'status':'linked','id':88,'type':'tv','season':1}}
        original=self.catalog.work
        self.catalog.work=lambda ident:dict(other) if ident=='other' else original(ident)
        with self.assertRaises(AppError) as error:self.app.workflow.create(self.user,'magnet:?xt=urn:btih:'+'a'*40,'Other - 42','other',
            naming={'work_id':'other','rules':releases.rules(),'release':releases.parse('Other - 42')})
        self.assertEqual(error.exception.code,'RESOURCE_WORK_CONFLICT')
        self.assertEqual(self.state.submit_count,1)

    def test_vps_mode_reuses_owned_cloud_file_without_a_second_transfer(self):
        tid=self.own42();body={**self.body('vps'),'release_ids':['c'*40]}
        plan=self.app.batch.action(self.user,body)
        self.assertEqual(plan['owned_episodes'],[])
        result=self.app.batch.action(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(result['tasks'][0]['id'],tid)
        self.assertEqual(self.state.submit_count,1)
        self.app.library.tick()
        local=self.store.one('SELECT target_path FROM library_items')
        self.assertEqual(Path(local['target_path']).read_bytes(),PAYLOAD)
        self.assertIn('anime',Path(local['target_path']).parts)
        plan=self.app.batch.action(self.user,self.body('vps'))
        self.assertEqual(plan['owned_episodes'],[42])
        self.assertEqual(plan['selected_episodes'],[41])

    def test_empty_selection_means_no_jobs_and_changed_inventory_invalidates_confirmation(self):
        body={**self.body(),'release_ids':[]};plan=self.app.batch.action(self.user,body)
        self.assertEqual(plan['items'],[])
        initial=self.app.batch.action(self.user,self.body())
        self.own42()
        with self.assertRaises(AppError) as failure:self.app.batch.action(self.user,{**self.body(),'digest':initial['digest']})
        self.assertEqual(failure.exception.code,'LIBRARY_PLAN_CHANGED')

    def test_only_existing_episodes_are_offered_for_upgrade_and_original_is_kept(self):
        self.own42();body=self.body(action='upgrade');plan=self.app.batch.action(self.user,body)
        self.assertEqual(plan['selected_episodes'],[42])
        result=self.app.batch.action(self.user,{**body,'digest':plan['digest']})
        self.app.workflow.tick();tid=result['tasks'][0]['id'];task=self.store.one('SELECT * FROM transfers WHERE id=?',(tid,))
        self.state.complete(task['alist_task_id']);self.state.files[task['task_cid']][0]['name']='file.S01E42.mp4'
        self.app.workflow.poll(tid,True);self.app.organizer.tick();self.app.layout.tick()
        self.assertEqual(len(self.store.all('SELECT * FROM cloud_outputs')),2)
        self.assertIn('版本',self.store.one('SELECT name FROM transfer_files WHERE transfer_id=?',(tid,))['name'])
