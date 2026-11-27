"""Package only portable code, public catalog metadata and deployment templates."""
import argparse,hashlib,json,pathlib,tarfile,time

root=pathlib.Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
output=a.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
if output.is_relative_to(root):raise SystemExit('Write the release outside the active application tree.')
skip={'node_modules','__pycache__','.venv','.git','data','runtime','release','test-results','source-reference',
      'logs','cache','.pytest_cache','.mypy_cache','.ruff_cache','.idea','.vscode'}
private_suffixes={'.pyc','.log','.key','.pem','.p12','.pfx','.db','.sqlite','.sqlite3'}
files=[]
for f in sorted(root.rglob('*')):
    rel=f.relative_to(root)
    if not f.is_file() or f.is_symlink() or any(x in skip for x in rel.parts):continue
    if f.name!='.env.example' and (f.name=='.env' or f.name.startswith('.env.')):continue
    if f.name.startswith('work-run-') and f.suffix=='.json':continue
    if rel.as_posix()!='seed/catalog.sqlite' and (f.suffix.lower() in private_suffixes
           or any(marker in f.name.lower() for marker in ('.sqlite-','.sqlite3-','.db-'))):continue
    files.append(f)
required=['AGENTS.md','README.md','docs/usage.md','.env.example','run.py','mikan115/server.py','mikan115/metadata.py',
          'mikan115/library.py','mikan115/batch.py','mikan115/mikan_parser.py','frontend/components.jsx',
          'frontend/review.css','templates/index.html','static/mikan115-ui.js','static/assets/mikan115-app.js',
          'seed/catalog.sqlite','compose.yaml','requirements.txt']
for path in required:
    if not (root/path).is_file():raise SystemExit('Required portable file missing: '+path)
manifest={'version':1,'created_at':time.time(),'real_115_login_performed':False,
          'includes_runtime_credentials':False,'files':[{'path':str(f.relative_to(root)).replace('\\','/'),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in files]}
with tarfile.open(output,'w:gz') as t:
    for f in files:
        info=t.gettarinfo(str(f),arcname='mikan115/'+str(f.relative_to(root)).replace('\\','/'))
        info.uid=info.gid=0;info.uname=info.gname='';info.mode=0o755 if f.suffix=='.sh' else 0o644
        with f.open('rb') as src:t.addfile(info,src)
checksum=hashlib.sha256(output.read_bytes()).hexdigest()
manifest['archive_sha256']=checksum;manifest['archive_bytes']=output.stat().st_size
output.with_suffix(output.suffix+'.manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
output.with_suffix(output.suffix+'.sha256').write_text(checksum+'  '+output.name+'\n')
print(json.dumps({'release':str(output),'files':len(files),'bytes':output.stat().st_size,'sha256':checksum}))
