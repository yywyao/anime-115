"""Create a fresh portable instance's private configuration; never import old accounts."""
import os
import argparse
from pathlib import Path
import secrets

parser=argparse.ArgumentParser(description='Create private local deployment values.')
parser.add_argument('--sonarr',action='store_true',help='enable the isolated read-only Sonarr profile')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
dest=root/'.env'
if dest.exists():raise SystemExit('.env already exists; refusing to replace credentials.')
uid=os.getuid() if hasattr(os,'getuid') else 10001
gid=os.getgid() if hasattr(os,'getgid') else 10001
if uid==0:uid=gid=10001
target=root/'runtime'/'app'
if not target.resolve().is_relative_to(root):raise SystemExit('Unsafe runtime directory')
target.mkdir(parents=True,exist_ok=True,mode=0o700);target.chmod(0o700)
if hasattr(os,'geteuid') and os.geteuid()==0:os.chown(target,uid,gid)
settings={'MIKAN_SETUP_TOKEN':secrets.token_urlsafe(32),'ALIST_PASSWORD':secrets.token_urlsafe(32),
          'SONARR_MANAGED':'1' if args.sonarr else '0','SONARR_URL':'http://sonarr:8989',
          'SONARR_API_VERSION':'v3','SONARR_PORT':'8989',
          'APP_UID':str(uid),'APP_GID':str(gid),'MIKAN_PORT':'8788','ALIST_PORT':'5244',
          'MIKAN_SECURE_COOKIE':'0','MIKAN_URL':'https://mikanani.me'}
fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w',encoding='utf-8') as f:
    f.write('# Private local deployment values. Never share or package this file.\n')
    for k,v in settings.items():f.write(k+'='+v+'\n')
print('Private configuration created:',dest)
print('No website account or 115 login was created. Read MIKAN_SETUP_TOKEN privately for first-run setup.')
if args.sonarr:print('Managed Sonarr enabled; start it with: docker compose --profile sonarr up -d')
