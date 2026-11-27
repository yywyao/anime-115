"""Run offline HTTP integration tests and save a credential-free verification record."""
import argparse,datetime,hashlib,json,pathlib,platform,sys,unittest
root=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
p=argparse.ArgumentParser();p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
class Record(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.records=[]
    def addSuccess(self,test):super().addSuccess(test);self.records.append({'name':test.id(),'result':'passed'})
    def addFailure(self,test,err):super().addFailure(test,err);self.records.append({'name':test.id(),'result':'failed'})
    def addError(self,test,err):super().addError(test,err);self.records.append({'name':test.id(),'result':'error'})
suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test_*.py',top_level_dir=str(root))
result=unittest.TextTestRunner(verbosity=2,resultclass=Record).run(suite)
files=[f for f in (root/'mikan115').glob('*.py')]+[root/'static/assets/mikan115-app.js',root/'static/mikan115-ui.js']
report={'verified_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'python':platform.python_version(),
        'tests_run':result.testsRun,'success':result.wasSuccessful(),'cases':result.records,
        'environment':'local-only HTTP mock services, artificial cookies and local test accounts',
        'real_115_login':False,'real_115_cloud_transfer':False,'real_alist_115_storage_login':False,
        'closed_loop':'Mikan mock page -> selected resource -> local account authorization -> 115 cloud task via AList protocol -> task polling -> AList scoped file listing -> authenticated proxy byte stream and hash/range checks',
        'file_hashes':{str(f.relative_to(root)).replace('\\','/'):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.exists()}}
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
sys.exit(0 if result.wasSuccessful() else 1)
