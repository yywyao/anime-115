import contextlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from .errors import AppError


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
 disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS binding (
 id INTEGER PRIMARY KEY CHECK(id=1), account_uid TEXT NOT NULL,
 account_name TEXT NOT NULL, cookie_encrypted TEXT NOT NULL,
 root_cid TEXT NOT NULL, root_name TEXT NOT NULL, mount_path TEXT NOT NULL,
 storage_id INTEGER NOT NULL, ready INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfers (
 id TEXT PRIMARY KEY, account_uid TEXT NOT NULL, infohash TEXT NOT NULL, magnet TEXT NOT NULL,
 title TEXT NOT NULL, work_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
 task_cid TEXT, task_path TEXT, alist_task_id TEXT, cloud_file_id TEXT,
 progress REAL NOT NULL DEFAULT 0, error_code TEXT, error_message TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_poll_at REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(account_uid, infohash));
CREATE TABLE IF NOT EXISTS transfer_owners (
 transfer_id TEXT NOT NULL REFERENCES transfers(id), user_id INTEGER NOT NULL REFERENCES users(id),
 PRIMARY KEY(transfer_id,user_id));
CREATE TABLE IF NOT EXISTS transfer_events (
 id INTEGER PRIMARY KEY, transfer_id TEXT NOT NULL REFERENCES transfers(id),
 kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_files (
 id TEXT PRIMARY KEY, transfer_id TEXT NOT NULL REFERENCES transfers(id),
 path TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, downloads INTEGER NOT NULL DEFAULT 0,
 UNIQUE(transfer_id,path));
CREATE TABLE IF NOT EXISTS subscriptions (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), work_id TEXT NOT NULL,
 bangumi_id TEXT NOT NULL, subgroup_id TEXT NOT NULL, title TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, initialized INTEGER NOT NULL DEFAULT 0,
 last_polled REAL NOT NULL DEFAULT 0, last_error TEXT,
 UNIQUE(user_id,bangumi_id,subgroup_id));
CREATE TABLE IF NOT EXISTS subscription_items (
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id), item_key TEXT NOT NULL,
 transfer_id TEXT, PRIMARY KEY(subscription_id,item_key));
CREATE TABLE IF NOT EXISTS catalog_cache (
 key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS favorites (
 user_id INTEGER NOT NULL REFERENCES users(id), work_id TEXT NOT NULL,
 PRIMARY KEY(user_id,work_id));
CREATE TABLE IF NOT EXISTS login_attempts (
 key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, reset_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_works (
 id TEXT PRIMARY KEY,mikan_id TEXT,title TEXT,year INTEGER,season TEXT,weekday INTEGER,
 payload TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_mikan ON catalog_works(mikan_id);
CREATE TABLE IF NOT EXISTS catalog_sync (
 key TEXT PRIMARY KEY,last_success REAL NOT NULL DEFAULT 0,next_due REAL NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0,details TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS work_groups (
 bangumi_id TEXT NOT NULL,group_id TEXT NOT NULL,name TEXT NOT NULL,seen_at REAL NOT NULL,
 PRIMARY KEY(bangumi_id,group_id));
CREATE TABLE IF NOT EXISTS group_publishers (
 bangumi_id TEXT NOT NULL,group_id TEXT NOT NULL,publisher_id TEXT NOT NULL,
 PRIMARY KEY(bangumi_id,group_id,publisher_id));
CREATE TABLE IF NOT EXISTS release_sources (
 resource_hash TEXT NOT NULL,bangumi_id TEXT NOT NULL,group_id TEXT NOT NULL,seen_at REAL NOT NULL,
 PRIMARY KEY(resource_hash,bangumi_id,group_id));
CREATE TABLE IF NOT EXISTS subscription_releases (
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id),item_key TEXT NOT NULL,payload TEXT NOT NULL,
 status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',transfer_id TEXT,first_seen REAL NOT NULL,updated_at REAL NOT NULL,
 PRIMARY KEY(subscription_id,item_key));
CREATE TABLE IF NOT EXISTS subscription_claims (
 user_id INTEGER NOT NULL REFERENCES users(id),episode_key TEXT NOT NULL,
 subscription_id TEXT NOT NULL,item_key TEXT NOT NULL,transfer_id TEXT,
 PRIMARY KEY(user_id,episode_key));
CREATE TABLE IF NOT EXISTS transfer_naming (
 transfer_id TEXT PRIMARY KEY REFERENCES transfers(id),metadata TEXT NOT NULL,auto_rename INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'PENDING',error TEXT,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS following_downloads (
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id), transfer_id TEXT NOT NULL REFERENCES transfers(id),
 revision INTEGER NOT NULL, submitted INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(subscription_id,transfer_id));
CREATE INDEX IF NOT EXISTS following_download_pending ON following_downloads(submitted,next_retry);
CREATE TABLE IF NOT EXISTS rename_operations (
 transfer_id TEXT NOT NULL REFERENCES transfers(id),file_id TEXT NOT NULL,old_path TEXT NOT NULL,new_path TEXT NOT NULL,
 size INTEGER NOT NULL,status TEXT NOT NULL,updated_at REAL NOT NULL,remote_id TEXT NOT NULL,parent_cid TEXT NOT NULL,
 PRIMARY KEY(transfer_id,file_id,old_path));
'''


class Store:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data/'mikan115.sqlite'
        key_file = self.data/'secret.key'
        if not key_file.exists():
            try:
                fd = os.open(key_file, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.fernet = Fernet(key_file.read_bytes().strip())
        with contextlib.closing(self.connect()) as c:
            c.executescript(SCHEMA)
            columns={r['name'] for r in c.execute('PRAGMA table_info(subscriptions)')}
            for name,definition in [('rules_json',"TEXT NOT NULL DEFAULT '{}'"),('source_encrypted',"TEXT NOT NULL DEFAULT ''"),('source_kind',"TEXT NOT NULL DEFAULT 'bangumi'"),('revision','INTEGER NOT NULL DEFAULT 1'),('removed','INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:c.execute('ALTER TABLE subscriptions ADD COLUMN '+name+' '+definition)
            rename_columns={r['name'] for r in c.execute('PRAGMA table_info(rename_operations)')}
            for name in ['remote_id','parent_cid']:
                if name not in rename_columns:c.execute('ALTER TABLE rename_operations ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")
            c.commit()
        self.path.chmod(0o600)
        with self.transaction() as c:
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('instance_id', secrets.token_hex(12)))

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=30000')
        return c

    @contextlib.contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def one(self, sql, args=()):
        with contextlib.closing(self.connect()) as c:
            row = c.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with contextlib.closing(self.connect()) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def setting(self, key, default=None):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row['value'] if row else default

    def set_setting(self, key, value):
        with self.transaction() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    @property
    def instance_id(self):
        return self.setting('instance_id')

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            raise AppError('SECRET_KEY_MISMATCH', '无法读取连接凭据，请检查数据卷与密钥文件是否配套', 503)

    def binding(self, require=True):
        row = self.one('SELECT * FROM binding WHERE id=1')
        if require and (not row or not row['ready']):
            raise AppError('CONNECTION_REQUIRED', '请管理员先在设置中连接 115 和 Alist', 409)
        return row

    def event(self, tid, kind, message):
        with self.transaction() as c:
            c.execute('INSERT INTO transfer_events(transfer_id,kind,message,created_at) VALUES(?,?,?,?)', (tid,kind,message,time.time()))

    def cache_get(self, key, stale=False):
        row = self.one('SELECT * FROM catalog_cache WHERE key=?', (key,))
        if row and (stale or row['expires_at'] > time.time()):
            return json.loads(row['payload'])
        return None

    def cache_set(self, key, data, ttl=300):
        with self.transaction() as c:
            c.execute('INSERT INTO catalog_cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at', (key,json.dumps(data,ensure_ascii=False),time.time()+ttl))
