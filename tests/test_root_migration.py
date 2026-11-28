import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from mikan115.errors import AppError,ProviderError
from mikan115.library import Library
from mikan115.library_paths import rename_directory
from tests import test_library as library_tests
from tests.test_workflow import Harness
from tests.mock_services import COOKIE


class LocalRootMigration(unittest.TestCase):
    setUp=library_tests.LibraryTests.setUp
    tearDown=library_tests.LibraryTests.tearDown

    def legacy(self):
        task=self.library.enqueue(self.user,'transfer-1',{'file_ids':['f1','f2']})
        self.library.run(task['task_id'])
        new=self.library.root/'anime';old=self.library.root/'mikan'
        rename_directory(new,old)
        with self.store.transaction() as db:
            for table,column in [('library_tasks','root_path'),('library_items','target_path')]:
                for row in list(db.execute(f'SELECT id,{column} FROM {table}')):
                    db.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(str(old/Path(row[column]).relative_to(new)),row['id']))
        return old,new

    def test_existing_files_and_database_paths_migrate_without_redownloading(self):
        old,new=self.legacy();inode=old.stat().st_ino;downloads=len(self.workflow.downloads)
        self.library.migrate_root()
        self.assertFalse(old.exists());self.assertEqual(new.stat().st_ino,inode)
        self.assertEqual(sorted(file.read_bytes() for file in new.rglob('*.mkv')),[b'one',b'tri'])
        self.assertTrue(all(Path(row['target_path']).is_relative_to(new) for row in self.store.all('SELECT target_path FROM library_items')))
        self.assertEqual(self.library.summary(self.user,['236'])['items']['236']['episodes'],[1,3])
        self.library.migrate_root();self.assertEqual(len(self.workflow.downloads),downloads)

    def test_interrupted_rename_recovers_same_directory_and_updates_old_records(self):
        old,new=self.legacy();inode=old.stat().st_ino
        def interrupted(source,target):rename_directory(source,target);raise OSError('simulated interrupted response')
        with patch('mikan115.library_paths.rename_directory',side_effect=interrupted),self.assertRaises(AppError):self.library.migrate_root()
        self.assertTrue(new.exists());self.assertFalse(old.exists());self.assertTrue(self.store.setting('library_root_rename'))
        fresh=Library(self.store,self.workflow,catalog=self.library.catalog);fresh.migrate_root()
        self.assertEqual(new.stat().st_ino,inode);self.assertIsNone(self.store.setting('library_root_rename'))
        self.assertTrue(all(Path(row['target_path']).is_file() for row in self.store.all('SELECT target_path FROM library_items')))

    def test_existing_foreign_target_is_not_merged_or_overwritten(self):
        old,new=self.legacy();new.mkdir();(new/'private.txt').write_bytes(b'keep')
        with self.assertRaises(AppError) as error:self.library.migrate_root()
        self.assertEqual(error.exception.code,'LIBRARY_ROOT_CONFLICT')
        self.assertEqual((new/'private.txt').read_bytes(),b'keep');self.assertEqual(len(list(old.rglob('*.mkv'))),2)

    def test_target_created_during_rename_is_not_replaced_even_when_empty(self):
        old,new=self.legacy();identity=[]
        def raced(source,target):target.mkdir();identity.append(target.stat().st_ino);rename_directory(source,target)
        with patch('mikan115.library_paths.rename_directory',side_effect=raced),self.assertRaises(AppError):self.library.migrate_root()
        self.assertTrue(old.exists());self.assertEqual(new.stat().st_ino,identity[0])

    def test_active_download_and_update_health_check_defer_migration(self):
        old,new=self.legacy()
        with self.store.transaction() as db:db.execute("UPDATE library_tasks SET status='DOWNLOADING',lease_until=?",(time.time()+60,))
        with self.assertRaises(AppError) as error:self.library.migrate_root()
        self.assertEqual(error.exception.code,'LIBRARY_MIGRATION_BUSY');self.assertTrue(old.exists())
        with self.store.transaction() as db:db.execute("UPDATE library_tasks SET status='COMPLETE',lease_until=0")
        self.store.set_setting('update_maintenance','1')
        with self.assertRaises(AppError):self.library.migrate_root()
        self.assertTrue(old.exists());self.assertFalse(new.exists())
        self.store.set_setting('update_maintenance','0');self.library.migrate_root();self.assertTrue(new.exists())

    @unittest.skipIf(os.name=='nt','Windows symlink permission is not required for the application')
    def test_symlink_root_cannot_move_private_directory(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir();(outside/'keep').write_bytes(b'private')
        (self.library.root/'mikan').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(AppError):self.library.migrate_root()
        self.assertEqual((outside/'keep').read_bytes(),b'private')


class CloudRootMigration(Harness):
    def legacy(self):
        self.bind();binding=self.store.binding()
        self.state.folders[binding['root_cid']]['name']='mikan'
        with self.store.transaction() as db:db.execute("UPDATE binding SET root_name='mikan'")
        return binding

    def test_cloud_root_keeps_same_id_files_and_alist_storage(self):
        before=self.legacy();child=self.state.mkdir(before['root_cid'],'Series')
        self.store.set_setting('connection_error','old migration error');self.store.set_setting('connection_error_code','CLOUD_ROOT_CONFLICT')
        self.app.workflow.tick();after=self.store.binding()
        self.assertIsNone(self.store.setting('connection_error'))
        self.assertEqual(after['root_cid'],before['root_cid']);self.assertEqual(after['storage_id'],before['storage_id'])
        self.assertEqual(after['root_name'],'anime');self.assertEqual(self.state.folders[child]['parent'],before['root_cid'])
        renamed=self.state.rename_count;self.app.workflow.scope();self.assertEqual(self.state.rename_count,renamed)
        self.assertEqual(self.state.folders['1']['name'],'DailyFiles')

    def test_cloud_rename_timeout_reconciles_without_repeating_the_rename(self):
        before=self.legacy();factory=self.app.workflow.pan_factory;once=[True]
        def wrapped(cookie):
            pan=factory(cookie);original=pan.rename_file
            def uncertain(fid,name):
                original(fid,name)
                if once[0]:once[0]=False;raise ProviderError('UNCERTAIN','simulated timeout',uncertain=True)
            pan.rename_file=uncertain;return pan
        self.app.workflow.pan_factory=wrapped
        with self.assertRaises(AppError):self.app.workflow.scope()
        self.assertEqual(self.store.binding()['root_name'],'mikan')
        renamed=self.state.rename_count;self.app.workflow.scope()
        self.assertEqual(self.store.binding()['root_name'],'anime');self.assertEqual(self.state.rename_count,renamed)

    def test_expired_cookie_waits_for_settings_update_before_retrying(self):
        self.legacy();self.state.expired=True
        with patch.object(self.app.workflow,'pan_factory',wraps=self.app.workflow.pan_factory) as factory:
            self.app.workflow.tick();calls=factory.call_count
            self.assertGreater(calls,0);self.assertEqual(self.store.setting('connection_error_code'),'COOKIE_EXPIRED')
            self.app.workflow.tick();self.assertEqual(factory.call_count,calls)
        self.assertEqual(self.store.binding()['root_name'],'mikan')
        self.state.expired=False;self.bind();self.assertEqual(self.store.binding()['root_name'],'anime')

    def test_unrelated_anime_directory_is_never_adopted(self):
        before=self.legacy();foreign=self.state.mkdir('0','anime')
        with self.assertRaises(AppError) as error:self.app.workflow.scope()
        self.assertEqual(error.exception.code,'CLOUD_ROOT_CONFLICT')
        self.assertEqual(self.store.binding()['root_cid'],before['root_cid'])
        self.assertEqual(self.state.folders[before['root_cid']]['name'],'mikan');self.assertEqual(self.state.folders[foreign]['name'],'anime')

    def test_external_rename_without_durable_intent_is_not_silently_adopted(self):
        before=self.legacy();self.state.folders[before['root_cid']]['name']='anime'
        with self.assertRaises(AppError):self.app.workflow.scope()
        self.assertEqual(self.store.binding()['root_name'],'mikan')


class DirectorySettings(Harness):
    def verify(self):
        status,data,_=self.request('POST','/api/settings/connection/verify',{'cookie':COOKIE})
        self.assertEqual(status,200,data);self.assertTrue(data['cookie_verified']);return data

    def save(self,name):return self.request('POST','/api/settings/connection/directory',{'name':name})

    def test_ck_verification_is_read_only_and_directory_is_a_separate_step(self):
        self.config.alist_password=''
        data=self.verify();self.assertFalse(data['ready']);self.assertEqual(data['directory_name'],'anime')
        self.assertEqual(set(self.state.folders),{'0','1'});self.assertEqual(self.state.storages,{})
        self.assertTrue(all(call[1]=='/115-user/' for call in self.state.calls))
        self.assertNotIn(COOKIE,json.dumps(data));self.assertNotIn(COOKIE,self.store.setting('pending_115_cookie'))
        self.config.alist_managed=True;self.config.alist_password='artificial-alist-password'
        self.app.workflow.tick();self.assertIsNone(self.store.binding(False))
        status,data,_=self.save('');self.assertEqual(status,200,data);self.assertEqual(data['transfer_folder'],'anime')
        self.assertEqual(self.state.submit_count,0)

    def test_default_name_conflict_does_not_invalidate_ck_or_adopt_private_files(self):
        foreign=self.state.mkdir('0','anime');self.verify()
        status,error,_=self.save('anime');self.assertEqual(status,409,error)
        self.assertEqual(error['code'],'DEDICATED_FOLDER_ALREADY_EXISTS');self.assertIsNone(self.store.binding(False))
        _,state,_=self.request('GET','/api/settings/connection');self.assertTrue(state['cookie_verified'])
        status,data,_=self.save('我的动画');self.assertEqual(status,200,data)
        self.assertEqual(data['transfer_folder'],'我的动画');self.assertNotEqual(self.store.binding()['root_cid'],foreign)
        self.assertEqual(self.state.folders[foreign]['name'],'anime')

    def test_explicit_name_edit_preserves_existing_root_and_recovers_external_rename(self):
        self.bind();before=self.store.binding();child=self.state.mkdir(before['root_cid'],'Existing series')
        self.state.folders[before['root_cid']]['name']='renamed-outside'
        self.verify();status,data,_=self.save('自定动画');self.assertEqual(status,200,data)
        after=self.store.binding();self.assertEqual(after['root_cid'],before['root_cid']);self.assertEqual(after['storage_id'],before['storage_id'])
        self.assertEqual(self.state.folders[child]['parent'],before['root_cid']);self.assertEqual(data['transfer_folder'],'自定动画')
        self.app.workflow.tick();self.app.workflow.scope();self.assertEqual(self.store.binding()['root_name'],'自定动画')
        self.assertEqual(json.loads(self.state.storages[after['storage_id']]['addition'])['cookie'],COOKIE)

    def test_missing_bound_directory_does_not_prevent_ck_verification(self):
        self.bind();before=self.store.binding();del self.state.folders[before['root_cid']]
        self.assertTrue(self.verify()['cookie_verified'])
        status,error,_=self.save('anime');self.assertEqual(status,409,error);self.assertEqual(error['code'],'FOLDER_SCOPE_MISMATCH')
        self.assertTrue(self.app.workflow.connection_status()['cookie_verified']);self.assertFalse(self.store.binding(False)['ready'])

    def test_directory_requires_verified_cookie_and_rejects_paths_and_extra_fields(self):
        status,error,_=self.save('anime');self.assertEqual(status,409,error);self.assertEqual(error['code'],'COOKIE_VERIFICATION_REQUIRED')
        self.verify()
        for name in ('../private','a/b','a\\b','.',None):
            status,_,_=self.save(name);self.assertEqual(status,400)
        status,_,_=self.request('POST','/api/settings/connection/directory',{'name':'anime','cid':'1'});self.assertEqual(status,400)
        status,_,_=self.request('POST','/api/settings/connection/directory',{'name':'anime'},csrf=False);self.assertEqual(status,403)
        self.assertEqual(self.state.folders['1']['name'],'DailyFiles')

    def test_invalid_cookie_cannot_be_marked_verified_or_create_a_directory(self):
        self.state.expired=True
        status,error,_=self.request('POST','/api/settings/connection/verify',{'cookie':COOKIE})
        self.assertEqual(status,502,error);self.assertEqual(error['code'],'COOKIE_EXPIRED')
        self.assertFalse(self.app.workflow.connection_status()['cookie_verified']);self.assertIsNone(self.store.binding(False))
        self.assertEqual(set(self.state.folders),{'0','1'})
