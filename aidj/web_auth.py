"""Telegram initData verification and opaque, revocable browser sessions."""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def verify_telegram(raw, bot_token, owner_id, now=None,*,allow_listener=False):
    now=time.time() if now is None else now
    if not isinstance(raw,str) or len(raw)>16384:
        raise AuthError('Открой приложение из бота.')
    pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True)
    data=dict(pairs)
    if len(pairs)!=len(data):
        raise AuthError('Некорректный вход.')
    signature=data.pop('hash','')
    check='\n'.join(k+'='+v for k,v in sorted(data.items()))
    secret=hmac.digest(b'WebAppData',bot_token.encode(),'sha256')
    expected=hmac.new(secret,check.encode(),'sha256').hexdigest()
    if not hmac.compare_digest(signature,expected):
        raise AuthError('Открой приложение из бота.')
    try:
        date=int(data['auth_date']);user=json.loads(data['user'])
        if date>now+60 or now-date>86400 or type(user['id']) is not int or user['id']<=0 or (not allow_listener and str(user['id'])!=str(owner_id)) or user.get('is_bot'):
            raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise AuthError('Вход устарел. Открой плеер из бота заново.' if allow_listener else 'Доступ только владельцу бота.') from None
    return user['id']


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db,*,role='admin',user_id=None):
    if role not in ('admin','listener'):raise AuthError('Некорректные права доступа.')
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM web_sessions WHERE expires<?',(time.time(),))
    db.execute('DELETE FROM web_access WHERE kind=\'session\' AND digest NOT IN (SELECT digest FROM web_sessions)')
    db.execute('INSERT INTO web_sessions VALUES (?,?)',(digest(token),time.time()+30*86400))
    db.execute('INSERT INTO web_access VALUES (?,?,?,?)',('session',digest(token),role,user_id));db.commit()
    return token


def session_valid(db,token):
    return session_access(db,token) is not None


def access(role,user_id=None):
    return {'role':role,'userId':user_id,'editLibrary':role=='admin','rateSongs':role=='admin',
            'discoverSongs':role=='admin','personalPlaylists':True}


def session_access(db,token):
    if not isinstance(token,str) or not token or len(token)>=200:return None
    row=db.execute('''SELECT a.role,a.user_id FROM web_sessions s LEFT JOIN web_access a
        ON a.kind='session' AND a.digest=s.digest WHERE s.digest=? AND s.expires>?''',(digest(token),time.time())).fetchone()
    if not row:return None
    role=row['role']
    return access(role,row['user_id']) if role in ('admin','listener') else None


def browser_code(db,*,role='admin',user_id=None):
    if role not in ('admin','listener'):raise AuthError('Некорректные права доступа.')
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM web_login_codes WHERE expires<?',(time.time(),))
    db.execute('DELETE FROM web_access WHERE kind=\'code\' AND digest NOT IN (SELECT digest FROM web_login_codes)')
    db.execute('INSERT INTO web_login_codes VALUES (?,?)',(digest(token),time.time()+300))
    db.execute('INSERT INTO web_access VALUES (?,?,?,?)',('code',digest(token),role,user_id));db.commit()
    return token


def redeem_code(db,token):
    if not isinstance(token,str) or len(token)>200:
        raise AuthError('Ссылка недействительна. Открой новую из приложения в Telegram.')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('''SELECT c.expires,a.role,a.user_id FROM web_login_codes c LEFT JOIN web_access a
            ON a.kind='code' AND a.digest=c.digest WHERE c.digest=?''',(digest(token),)).fetchone()
        if not row or row['expires']<time.time():
            raise AuthError('Ссылка устарела. Открой новую из приложения в Telegram.')
        role=row['role'];user_id=row['user_id']
        if role not in ('admin','listener'):raise AuthError('Некорректные права доступа.')
        db.execute('DELETE FROM web_login_codes WHERE digest=?',(digest(token),))
        db.execute("DELETE FROM web_access WHERE kind='code' AND digest=?",(digest(token),));db.commit()
    except BaseException:
        db.rollback();raise
    return create_session(db,role=role,user_id=user_id)
