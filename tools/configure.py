"""Initialize private deployment values without replacing existing credentials."""
import os
import argparse
from pathlib import Path
import secrets

def configure(root):
    root=Path(root).resolve()
    dest=root/'.env'
    uid=os.getuid() if hasattr(os,'getuid') else 10001
    gid=os.getgid() if hasattr(os,'getgid') else 10001
    if uid==0:uid=gid=10001
    target=root/'runtime'/'app'
    if not target.resolve().is_relative_to(root):raise SystemExit('Unsafe runtime directory')
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    target.chmod(0o700)
    if hasattr(os,'geteuid') and os.geteuid()==0:os.chown(target,uid,gid)
    if dest.exists():
        print('Existing private configuration retained.')
        return
    settings={
        'MIKAN_SETUP_TOKEN':secrets.token_urlsafe(32),
        'ALIST_PASSWORD':secrets.token_urlsafe(32),
        'APP_UID':str(uid),'APP_GID':str(gid),'MIKAN_PORT':'8788',
        'MIKAN_SECURE_COOKIE':'0','MIKAN_URL':'https://mikanani.me',
    }
    try:fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        print('Existing private configuration retained.')
        return
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as output:
        output.write('# Private deployment values. Never share or package this file.\n')
        for key,value in settings.items():output.write(key+'='+value+'\n')
    print('Private deployment configuration created.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Initialize private deployment values.')
    parser.add_argument('--sonarr',action='store_true',help=argparse.SUPPRESS)
    parser.parse_args()
    configure(Path(__file__).resolve().parents[1])
