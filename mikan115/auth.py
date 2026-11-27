import hashlib
import hmac
import re
import secrets
import time

from .errors import AppError


def password_hash(password, *, salt=None):
    if not isinstance(password,str) or not 12 <= len(password) <= 256:
        raise AppError('WEAK_PASSWORD', '密码长度须为 12–256 个字符')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def password_matches(password, stored):
    try:
        kind, rounds, salt, expected = stored.split('$')
        if kind != 'pbkdf2_sha256' or int(rounds) != 600000 or len(password) > 256:
            return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)).hex()
        return hmac.compare_digest(expected,actual)
    except (ValueError,TypeError,AttributeError):
        return False


def public_user(row):
    return {'id':row['id'],'user_id':str(row['id']),'username':row['username'],
            'is_admin':bool(row['is_admin']),'user_type':'admin' if row['is_admin'] else 'user','status':'ACTIVE'}


class Auth:
    def __init__(self, store, config):
        self.store,self.config=store,config

    def setup_required(self):
        return not self.store.one('SELECT id FROM users LIMIT 1')

    def create_user(self, username, password, *, admin=False, first=False, setup_token=''):
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_\-\u4e00-\u9fff]{3,40}',username):
            raise AppError('INVALID_USERNAME','用户名须为 3–40 个汉字、字母、数字、下划线或短横线')
        hashed=password_hash(password)
        with self.store.transaction() as c:
            if first:
                if c.execute('SELECT id FROM users LIMIT 1').fetchone():
                    raise AppError('SETUP_COMPLETE','网站已完成初始化',409)
                expected=self.config.setup_token or self.store.setting('setup_token','')
                if not expected or not hmac.compare_digest(str(setup_token),expected):
                    raise AppError('SETUP_TOKEN_REQUIRED','初始化口令不正确',403)
            try:
                cur=c.execute('INSERT INTO users(username,password_hash,is_admin,created_at) VALUES(?,?,?,?)',(username,hashed,int(admin),time.time()))
            except Exception as e:
                if 'UNIQUE' in str(e):raise AppError('USERNAME_EXISTS','用户名已存在',409)
                raise
        return public_user(self.store.one('SELECT * FROM users WHERE id=?',(cur.lastrowid,)))

    def login(self,username,password,ip=''):
        key=hashlib.sha256((ip+'\0'+str(username).casefold()).encode()).hexdigest()
        now=time.time();attempt=self.store.one('SELECT * FROM login_attempts WHERE key=?',(key,))
        if attempt and attempt['reset_at']>now and attempt['failures']>=8:
            raise AppError('LOGIN_RATE_LIMIT','尝试次数过多，请 15 分钟后再试',429)
        row=self.store.one('SELECT * FROM users WHERE username=? COLLATE NOCASE',(str(username),))
        valid=row and password_matches(str(password),row['password_hash']) and not row['disabled']
        if not row:
            hashlib.pbkdf2_hmac('sha256',str(password)[:256].encode(),b'unknown-local-user',600000)
        if not valid:
            with self.store.transaction() as c:
                if not attempt or attempt['reset_at']<=now:
                    c.execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET failures=1,reset_at=excluded.reset_at',(key,1,now+900))
                else:c.execute('UPDATE login_attempts SET failures=failures+1 WHERE key=?',(key,))
            raise AppError('INVALID_CREDENTIALS','用户名或密码不正确',401)
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with self.store.transaction() as c:
            c.execute('DELETE FROM login_attempts WHERE key=?',(key,))
            c.execute('DELETE FROM sessions WHERE expires_at<=?',(now,))
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,now+self.config.session_hours*3600))
        return token,csrf,public_user(row)

    def session(self,token):
        if not token:return None
        row=self.store.one('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0',(hashlib.sha256(token.encode()).hexdigest(),time.time()))
        return {'user':public_user(row),'csrf':row['csrf']} if row else None

    def logout(self,token):
        with self.store.transaction() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self,uid,old,new):
        row=self.store.one('SELECT * FROM users WHERE id=?',(uid,))
        if not row or not password_matches(old,row['password_hash']):raise AppError('INVALID_PASSWORD','原密码不正确',403)
        hashed=password_hash(new)
        with self.store.transaction() as c:
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(hashed,uid))
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
