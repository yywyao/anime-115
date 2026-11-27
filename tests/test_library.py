import io
import json
from pathlib import Path
import tempfile
import unittest

from mikan115.errors import AppError
from mikan115.library import Library
from mikan115.store import Store


class Stream(io.BytesIO):
    status=200
    headers={}


class Workflow:
    def __init__(self,files,contents):
        self.files=files;self.contents=contents;self.downloads=[]

    def get(self,user,tid):
        if tid!='transfer-1':raise AppError('TASK_NOT_FOUND','任务不存在',404)
        return {'id':tid,'work_id':'236','infohash':'a'*40,'title':'测试动漫','state':'FILES_READY','files':self.files}

    def download(self,user,tid,file_id,range_header=None):
        self.downloads.append((user,tid,file_id))
        file=next(item for item in self.files if str(item['id'])==str(file_id))
        return file,Stream(self.contents[str(file_id)])


class Catalog:
    def work(self,mikan_id):
        return {'episodes':[{'season_number':1,'episode_number':1,'air_date':'2025-01-01'},{'season_number':1,'episode_number':2,'air_date':'2025-01-02'},{'season_number':1,'episode_number':3,'air_date':'2025-01-03'}]}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'state')
        self.files=[{'id':'f1','name':'Test.S01E01.mkv','size':3},{'id':'f2','name':'Test.S01E03.mkv','size':3}]
        self.workflow=Workflow(self.files,{'f1':b'one','f2':b'tri'})
        self.library=Library(self.store,self.workflow,catalog=Catalog())
        with self.store.transaction() as db:
            db.execute("INSERT INTO transfers(id,account_uid,infohash,magnet,title,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",('transfer-1','7','a'*40,'magnet:?xt=urn:btih:'+'a'*40,'测试动漫','FILES_READY',0,0))
            db.execute('INSERT INTO transfer_naming(transfer_id,metadata,auto_rename,status,error,updated_at) VALUES(?,?,?,?,?,?)',('transfer-1',json.dumps({'bangumi_id':'236','resource_hash':'r'*40}),0,'PENDING',None,0))
        self.user={'id':7,'is_admin':False}

    def tearDown(self):self.temp.cleanup()

    def test_queue_is_persistent_and_summary_uses_explicit_file_ids(self):
        task=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1','f2']})
        self.assertEqual(task['status'],'queued');self.assertEqual(task['episodes'],[1,3]);self.assertEqual(task['missing_episodes'],[2])
        done=self.library.run(task['task_id'])
        self.assertEqual(done['status'],'complete');self.assertEqual(done['episodes'],[1,3])
        self.assertTrue(all(item['status']=='COMPLETE' for item in done['files']))
        summary=self.library.summary(self.user,['236'])['items']['236']
        self.assertEqual(summary['status'],'partial');self.assertEqual(summary['episodes'],[1,3]);self.assertEqual(summary['missing_episodes'],[2]);self.assertEqual(summary['expected_episodes'],[1,2,3]);self.assertEqual(summary['task_ids'],[task['task_id']])
        self.assertEqual(summary['release_hashes'],['r'*40]);self.assertNotIn('target_path',done['files'][0])
        self.assertEqual(sorted(call[2] for call in self.workflow.downloads),['f1','f2'])

    def test_no_vps_task_reports_idle_availability(self):
        idle=self.library.task(self.user,'transfer-1')
        self.assertEqual(idle['state'],'IDLE');self.assertTrue(idle['available'])

    def test_summary_counts_only_real_video_files_and_supports_episode_formats(self):
        self.files.extend([{'id':'f3','name':'Test [02].mp4','size':3},{'id':'f4','name':'Test OP01.mp4','size':3},{'id':'f5','name':'Test [03].ass','size':3}])
        self.workflow.contents.update({'f3':b'two','f4':b'op!','f5':b'ass'})
        task=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1','f2','f3','f4','f5']})
        self.library.run(task['task_id']);summary=self.library.summary(self.user,['236'])['items']['236']
        self.assertEqual(summary['episodes'],[1,2,3]);self.assertEqual(summary['status'],'complete')

    def test_failed_enqueue_is_idempotent_and_requeued(self):
        self.workflow.contents['f1']=b'wrong'
        first=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']});self.assertEqual(self.library.run(first['task_id'])['status'],'failed')
        self.workflow.contents['f1']=b'one';second=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']})
        self.assertEqual(second['task_id'],first['task_id']);self.assertEqual(second['status'],'queued');self.assertEqual(self.library.run(first['task_id'])['status'],'complete')

    def test_completed_task_accepts_incremental_file_ids_without_new_task(self):
        first=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']})
        self.assertEqual(self.library.run(first['task_id'])['status'],'complete')
        second=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1','f2']})
        self.assertEqual(second['task_id'],first['task_id']);self.assertEqual(second['status'],'queued')
        self.assertEqual(self.library.run(first['task_id'])['status'],'complete')
        self.assertEqual(len(self.store.all('SELECT * FROM library_items WHERE library_task_id=?',(first['task_id'],))),2)

    def test_following_revision_and_pause_are_checked_at_queue_transaction(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO users(id,username,password_hash,is_admin,disabled,created_at) VALUES(?,?,?,?,?,?)",(7,'rss-user','hash',0,0,0))
            db.execute("INSERT INTO subscriptions(id,user_id,work_id,bangumi_id,subgroup_id,title,enabled,initialized,last_polled,rules_json,source_encrypted,source_kind,revision,removed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('sub-1',7,'236','236','7','测试',1,1,0,'{}','','bangumi',4,0))
        queued=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']},following=('sub-1',4))
        self.assertIsNotNone(queued)
        self.store.set_setting('rss_paused','1')
        self.assertIsNone(self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1','f2']},following=('sub-1',4)))

    def test_tick_processes_claimed_queue_once(self):
        task=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']})
        result=self.library.tick(1)
        self.assertEqual(result[0]['task_id'],task['task_id']);self.assertEqual(result[0]['status'],'complete')
        self.assertEqual(len(self.workflow.downloads),1)

    def test_size_mismatch_does_not_commit_partial_file(self):
        self.workflow.contents['f1']=b'wrong-size'
        task=self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['f1']})
        result=self.library.run(task['task_id'])
        self.assertEqual(result['status'],'failed');self.assertFalse([p for p in self.library.root.rglob('*') if p.is_file()])

    def test_no_paths_or_unconfirmed_mikan_id_are_accepted(self):
        with self.assertRaises(AppError) as error:
            self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['/etc/passwd']})
        self.assertEqual(error.exception.code,'LIBRARY_FILE_ID_INVALID')
        with self.assertRaises(AppError) as error:
            self.library.enqueue(self.user,'transfer-1',{'mikan_id':'999','file_ids':['f1']})
        self.assertEqual(error.exception.code,'LIBRARY_MIKAN_ID_REQUIRED')

    def test_untrusted_filename_is_rejected_before_queue_creation(self):
        self.workflow.files=[{'id':'bad','name':'../secret.mkv','size':1}]
        self.workflow.contents['bad']=b'x'
        with self.assertRaises(AppError) as error:
            self.library.enqueue(self.user,'transfer-1',{'mikan_id':'236','file_ids':['bad']})
        self.assertEqual(error.exception.code,'UNSAFE_LIBRARY_NAME')
        self.assertIsNone(self.store.one('SELECT * FROM library_tasks'))


if __name__=='__main__':unittest.main()
