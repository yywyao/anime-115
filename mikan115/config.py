from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


@dataclass
class Config:
    root: Path
    data: Path
    host: str = '127.0.0.1'
    port: int = 8788
    alist_url: str = 'http://127.0.0.1:5244'
    alist_username: str = 'admin'
    alist_password: str = ''
    alist_token: str = ''
    mikan_url: str = 'https://mikanani.me'
    secure_cookie: bool = False
    setup_token: str = ''
    worker_interval: float = 5
    rss_interval: int = 600
    session_hours: int = 24
    catalog_auto_update: bool = False
    alist_managed: bool = False
    sonarr_managed: bool = False
    sonarr_url: str = 'http://127.0.0.1:8989'
    sonarr_api_key: str = ''
    sonarr_api_version: str = 'v3'

    @staticmethod
    def managed_sonarr_key(alist_password: str) -> str:
        return hashlib.sha256(('mikan115-sonarr-v1:' + alist_password).encode('utf-8')).hexdigest()

    @classmethod
    def from_env(cls):
        root = Path(__file__).resolve().parents[1]
        return cls(root=root, data=Path(os.environ.get('MIKAN_DATA_DIR', root/'data')),
                   host=os.environ.get('MIKAN_HOST', '127.0.0.1'), port=int(os.environ.get('MIKAN_PORT', '8788')),
                   alist_url=os.environ.get('ALIST_URL', 'http://127.0.0.1:5244').rstrip('/'),
                   alist_username=os.environ.get('ALIST_USERNAME', 'admin'),
                   alist_password=os.environ.get('ALIST_PASSWORD', ''), alist_token=os.environ.get('ALIST_TOKEN', ''),
                   mikan_url=os.environ.get('MIKAN_URL', 'https://mikanani.me').rstrip('/'),
                   secure_cookie=os.environ.get('MIKAN_SECURE_COOKIE', '0') == '1',
                   setup_token=os.environ.get('MIKAN_SETUP_TOKEN', ''),
                   worker_interval=float(os.environ.get('MIKAN_WORKER_INTERVAL', '5')),
                   rss_interval=max(60, int(os.environ.get('MIKAN_RSS_INTERVAL', '600'))),
                   catalog_auto_update=os.environ.get('MIKAN_CATALOG_AUTO_UPDATE','1')=='1',
                   alist_managed=os.environ.get('ALIST_MANAGED','0')=='1',
                   sonarr_managed=os.environ.get('SONARR_MANAGED','0')=='1',
                   sonarr_url=os.environ.get('SONARR_URL','http://127.0.0.1:8989').rstrip('/'),
                   sonarr_api_key=cls.managed_sonarr_key(os.environ.get('ALIST_PASSWORD','')) if os.environ.get('SONARR_MANAGED','0')=='1' and os.environ.get('ALIST_PASSWORD','') else '',
                   sonarr_api_version=os.environ.get('SONARR_API_VERSION','v3'))
