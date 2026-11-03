"""Telegram initData verification and opaque, revocable browser sessions."""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def verify_telegram(raw, bot_token, owner_id, now=None):
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
        if date>now+60 or now-date>86400 or str(user['id'])!=str(owner_id) or user.get('is_bot'):
            raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise AuthError('Доступ только владельцу бота.') from None
    return user['id']


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db):
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM web_sessions WHERE expires<?',(time.time(),))
    db.execute('INSERT INTO web_sessions VALUES (?,?)',(digest(token),time.time()+30*86400));db.commit()
    return token


def session_valid(db,token):
    return bool(token and len(token)<200 and db.execute('SELECT 1 FROM web_sessions WHERE digest=? AND expires>?',
                                                       (digest(token),time.time())).fetchone())


def browser_code(db):
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM web_login_codes WHERE expires<?',(time.time(),))
    db.execute('INSERT INTO web_login_codes VALUES (?,?)',(digest(token),time.time()+300));db.commit()
    return token


def redeem_code(db,token):
    if not isinstance(token,str) or len(token)>200:
        raise AuthError('Ссылка недействительна. Открой новую из приложения в Telegram.')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT expires FROM web_login_codes WHERE digest=?',(digest(token),)).fetchone()
        if not row or row[0]<time.time():
            raise AuthError('Ссылка устарела. Открой новую из приложения в Telegram.')
        db.execute('DELETE FROM web_login_codes WHERE digest=?',(digest(token),));db.commit()
    except BaseException:
        db.rollback();raise
    return create_session(db)
