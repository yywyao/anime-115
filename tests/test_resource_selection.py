import json
from tests.test_workflow import Harness


class ResourceSelection(Harness):
    def setUp(self):
        super().setUp()
        def resource(letter,episode,resolution,version):
            return {'resourceHash':letter*40,'title':f'动画 - {episode:02d} [{resolution}][{version}]',
                    'magnetUrl':'magnet:?xt=urn:btih:'+letter*40}
        self.groups=[
            {'subgroupId':'7','subgroupName':'甲组','resources':[
                resource('a',1,'1080p','简日'),resource('b',2,'720p','简日')]},
            {'subgroupId':'8','subgroupName':'乙组','resources':[
                resource('c',1,'1080p','简日'),resource('d',2,'1080p','繁中')]},
        ]
        self.catalog.resources=lambda *args,**kwargs:{'groups':self.groups,'bangumi_id':'236'}
        self.catalog.work=lambda *args,**kwargs:{
            'title':'动画','mikan_id':'236','tmdb_season':1,
            'tmdb_mapping':{'status':'linked','type':'tv','id':77,'season':1},
        }

    def test_batch_revalidates_group_resolution_and_subtitle_on_server(self):
        self.bind()
        body={'mikan_id':'236','release_ids':['a'*40,'b'*40],
              'rules':{'group_ids':['7'],'resolutions':['1080p'],'versions':['chs-ja']}}
        status,result,_=self.request('POST','/api/transfers',body)
        self.assertEqual(status,409,result)
        self.assertEqual(result['code'],'RESOURCE_FILTER_MISMATCH')
        self.assertEqual(self.store.all('SELECT * FROM transfers'),[])
        body['release_ids']=['a'*40]
        status,result,_=self.request('POST','/api/transfers',body)
        self.assertEqual(status,202,result)
        self.assertEqual(len(result['tasks']),1)
        metadata=json.loads(self.store.one('SELECT metadata FROM transfer_naming')['metadata'])
        self.assertEqual(metadata['release']['group_id'],'7')
        self.assertEqual(metadata['rules']['versions'],['chs-ja'])
        self.assertEqual(self.state.submit_count,0)
        self.groups[0]['resources'].append(self.groups[1]['resources'][0])
        # Explicit hash-only policy permits an alternate release of the same
        # episode, so this assertion isolates multi-group provenance.
        result=self.app.batch.selected(self.user,'236',['c'*40],{'dedupe':'hash'})
        row=self.store.one('SELECT metadata FROM transfer_naming WHERE transfer_id=?',(result['tasks'][0]['id'],))
        duplicate=json.loads(row['metadata'])
        self.assertEqual({source['group_id'] for source in duplicate['sources']},{'7','8'})
        self.assertEqual(duplicate['release']['group_id'],'')

    def test_library_plan_uses_current_filters_and_selected_resource_ids(self):
        body={'mikan_id':'236','action':'complete',
              'rules':{'group_ids':['7'],'resolutions':['1080p'],'versions':['chs-ja']}}
        status,plan,_=self.request('POST','/api/library/actions',body)
        self.assertEqual(status,200,plan)
        self.assertEqual([item['release_id'] for item in plan['items']],['a'*40])
        body.update(rules={'versions':['chs-ja']},release_ids=['c'*40])
        status,selected,_=self.request('POST','/api/library/actions',body)
        self.assertEqual(status,200,selected)
        self.assertEqual([item['release_id'] for item in selected['items']],['c'*40])
        self.bind()
        body.update(release_ids=['a'*40],digest=selected['digest'])
        status,result,_=self.request('POST','/api/library/actions',body)
        self.assertEqual(status,409,result)
        self.assertEqual(result['code'],'LIBRARY_PLAN_CHANGED')
        self.assertEqual(self.store.all('SELECT * FROM transfers'),[])

    def test_group_specific_rss_rejects_other_group_rules_when_creating_and_editing(self):
        self.state.rss_items=[]
        status,result,_=self.request('POST','/api/subscriptions',{
            'bangumi_id':'236','subgroup_id':'7','rules':{'group_ids':['8']}})
        self.assertEqual(status,400,result)
        self.assertEqual(result['code'],'FEED_GROUP_MISMATCH')
        status,result,_=self.request('POST','/api/subscriptions',{
            'bangumi_id':'236','subgroup_id':'7','rules':{'resolutions':['1080p'],'versions':['chs-ja']}})
        self.assertEqual(status,201,result)
        saved=self.store.one('SELECT * FROM subscriptions WHERE id=?',(result['id'],))
        self.assertIn('subgroupid=7',self.store.decrypt(saved['source_encrypted']))
        self.assertEqual(json.loads(saved['rules_json'])['group_ids'],['7'])
        status,changed,_=self.request('POST','/api/subscriptions/'+result['id'],{'rules':{'group_ids':['8']}})
        self.assertEqual(status,400,changed)
        self.assertEqual(changed['code'],'FEED_GROUP_MISMATCH')

    def test_group_rss_follows_only_matching_future_releases(self):
        self.bind()
        self.state.following_demo=True
        self.state.rss_items=[]
        result=self.app.rss.create(self.user,{'bangumi_id':'236','subgroup_id':'7',
            'rules':{'resolutions':['1080p'],'versions':['chs-ja'],'wait_minutes':0}})
        sub=self.store.one('SELECT * FROM subscriptions WHERE id=?',(result['id'],))
        self.state.rss_items=[
            {'hash':'b'*40,'title':'[测试字幕组] 动画 - 01 [1080p][简日]'},
            {'hash':'c'*40,'title':'[测试字幕组] 动画 - 02 [720p][简日]'},
            {'hash':'d'*40,'title':'[第二字幕组] 动画 - 03 [1080p][简日]'},
            {'hash':'e'*40,'title':'[测试字幕组] 动画 - 04 [1080p][繁中]'},
        ]
        self.app.rss.poll(sub)
        tasks=self.store.all('SELECT * FROM transfers')
        self.assertEqual([task['infohash'] for task in tasks],['b'*40])
        calls=[call for call in self.state.calls if call[1]=='/RSS/Bangumi']
        self.assertTrue(calls)
        self.assertTrue(all(call[2].get('subgroupid')==['7'] for call in calls))
        self.assertEqual(self.app.rss.settings()['interval_seconds'],60)
