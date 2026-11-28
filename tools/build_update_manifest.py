"""Publish an API-independent manifest for a committed, sanitized code revision."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from mikan115.updater import Updates,managed_path,REPOSITORY


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--revision',default='HEAD');args=parser.parse_args()
    def git(*arguments):return subprocess.check_output(['git',*arguments],cwd=root)
    version=git('rev-parse',args.revision+'^{commit}').decode().strip()
    rows=[]
    for entry in git('ls-tree','-r','-l','-z',version).split(b'\0'):
        if not entry:continue
        info,path=entry.split(b'\t',1);name=path.decode('utf-8')
        if not managed_path(name):continue
        mode,kind,sha,size=info.decode().split()
        rows.append({'path':name,'mode':mode,'type':kind,'sha':sha,'size':int(size)})
    manifest=Updates.validate_manifest(version,rows)
    target=root/'seed'/'update-manifest.json'
    target.write_bytes((json.dumps({'protocol':1,'repository':REPOSITORY,**manifest},indent=2,sort_keys=True)+'\n').encode('utf-8'))
    print(json.dumps({'version':version,'files':len(manifest['files']),'output':'seed/update-manifest.json'}))


if __name__=='__main__':main()
