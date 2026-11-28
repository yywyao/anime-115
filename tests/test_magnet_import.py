import base64
import contextlib
import http.client
import json
import urllib.parse
from mikan115.errors import AppError
from mikan115.magnet_import import extract
from tests.test_workflow import Harness


def magnet(letter,title=''):
    return 'magnet:?'+urllib.parse.urlencode({'xt':'urn:btih:'+letter*40,**({'dn':title} if title else {})})


class MagnetImports(Harness):
    def setUp(self):
        super().setUp();self.bind()
        self.catalog.save_works([{'id':'linked-work','mikan_id':'236','title':'测试动漫','series_title':'测试动漫','tmdb_id':77,'tmdb_season':1,
            'tmdb_mapping':{'status':'linked','id':77,'type':'tv','season':1},'episodes':[
                {'season_number':1,'episode_number':number,'air_date':'2020-01-01'} for number in range(1,13)]}])

    def test_multiple_links_parse_groups_versions_and_duplicate_btih(self):
        first=magnet('a','[FSD] 测试动漫 - 01 [1080p][简繁日]')
        second=magnet('b','[FSD] 测试动漫 - 02 [1080p][简繁日]')
        items,invalid,duplicates=extract(first+'\n'+second+'\n'+first+'\nmagnet:?xt=urn:btih:wrong')
        self.assertEqual(len(items),2);self.assertEqual(duplicates,1);self.assertEqual(len(invalid),1)
        self.assertEqual(items[0]['group_key'],items[1]['group_key'])
        self.assertEqual(items[0]['release']['group'],'FSD');self.assertEqual(items[1]['release']['episodes'],[2])
        self.assertEqual(items[0]['release']['version'],'chs-cht-ja')
        encoded=base64.b32encode(bytes.fromhex('a'*40)).decode()
        self.assertEqual(extract(first+'\nmagnet:?xt=urn:btih:'+encoded)[2],1)
        unnamed_first=extract(magnet('a')+'\n'+first)[0][0]
        self.assertTrue(unnamed_first['named']);self.assertFalse(unnamed_first['label_conflict'])
        self.assertEqual(len(extract(first+second)[0]),2)

    def test_preview_has_no_cloud_side_effect_and_unknowns_are_not_guessed(self):
        text=magnet('a')+'\n'+magnet('b','[Other] Something [1080p]')
        plan=self.app.magnet_import.plan(self.user,{'text':text})
        self.assertEqual(plan['selected_count'],2);self.assertEqual(self.state.submit_count,0)
        self.assertEqual(plan['items'][0]['release']['episodes'],[])
        self.assertEqual(plan['items'][0]['work_id'],'')
        self.assertEqual(self.store.all('SELECT * FROM transfers'),[])

    def test_confirmed_work_merges_same_episode_mirrors_into_one_task(self):
        body={'text':magnet('a','[FSD] 测试动漫 - 01 [1080p][简日]')+'\n'+magnet('b','[Other] 测试动漫 - 01 [1080p][简日]')}
        plan=self.app.magnet_import.plan(self.user,body)
        body['bindings']={item['group_key']:'linked-work' for item in plan['items']}
        plan=self.app.magnet_import.plan(self.user,body)
        result=self.app.magnet_import.plan(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(len(result['tasks']),1);self.assertEqual(result['merged'],1)
        self.app.workflow.tick();self.assertEqual(self.state.submit_count,1)

    def test_raw_hashes_are_not_merged_by_similar_titles_or_guessed_ids(self):
        body={'text':magnet('a','[FSD] 测试动漫 - 01')+'\n'+magnet('b','[Other] 测试动漫 - 01')}
        plan=self.app.magnet_import.plan(self.user,body);result=self.app.magnet_import.plan(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(len(result['tasks']),2);self.assertTrue(all(task['work_id']=='' for task in result['tasks']))

    def test_empty_selection_does_not_create_tasks_and_vps_intent_is_separate(self):
        body={'text':magnet('a','[FSD] 测试动漫 - 01'),'selected':[]}
        plan=self.app.magnet_import.plan(self.user,body);result=self.app.magnet_import.plan(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(result['tasks'],[])
        body={'text':body['text'],'destination':'vps'};plan=self.app.magnet_import.plan(self.user,body)
        result=self.app.magnet_import.plan(self.user,{**body,'digest':plan['digest']})
        self.assertEqual(len(result['downloads']),1);self.assertEqual(self.state.submit_count,0)
        self.assertEqual(len(self.store.all('SELECT * FROM library_requests')),1)

    def test_bulk_http_requires_csrf_and_revalidates_input(self):
        body={'text':magnet('a')};status,_,_=self.request('POST','/api/transfers/import',body,csrf=False);self.assertEqual(status,403)
        status,plan,_=self.request('POST','/api/transfers/import',body);self.assertEqual(status,200)
        status,_,_=self.request('POST','/api/transfers/import',{'text':magnet('b'),'digest':plan['digest']});self.assertEqual(status,409)
        with self.assertRaises(AppError):extract('\n'.join(magnet('a') for _ in range(101)))

    def test_conflicting_names_for_one_magnet_require_review(self):
        plan=self.app.magnet_import.plan(self.user,{'text':magnet('a','[FSD] Show - 01')+'\n'+magnet('a','[Other] Other Show - 02')})
        self.assertEqual(plan['selected_count'],0);self.assertEqual(plan['items'][0]['reason'],'同一磁力的名称冲突')

    def test_nameless_link_can_reuse_exact_hash_labels_without_guessing_work_id(self):
        self.app.workflow.create(self.user,magnet('a'),'[FSD] 测试动漫 - 01 [1080p][简日]',enqueue_only=True)
        plan=self.app.magnet_import.plan(self.user,{'text':magnet('a')})
        self.assertEqual(plan['items'][0]['release']['group'],'FSD')
        self.assertEqual(plan['items'][0]['release']['episodes'],[1]);self.assertEqual(plan['items'][0]['work_id'],'')

    def test_bulk_route_accepts_bounded_large_paste_without_raising_other_route_limits(self):
        status,plan,_=self.request('POST','/api/transfers/import',{'text':magnet('a')+'\n'*33000})
        self.assertEqual(status,200,plan);self.assertEqual(plan['selected_count'],1)
        status,_,_=self.request('POST','/api/transfers',{'magnet':magnet('a'),'padding':'x'*40000})
        self.assertEqual(status,413)
        with contextlib.closing(http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)) as connection:
            connection.request('POST','/api/transfers',json.dumps({'padding':'x'*40000}),{'Content-Type':'application/json'})
            response=connection.getresponse();self.assertEqual(response.status,413);response.read()
            connection.request('GET','/api/health');response=connection.getresponse();self.assertEqual(response.status,200);response.read()
