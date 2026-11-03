"""Credential setup that never loses an entered token on a network failure."""
import requests
import time

from .config import atomic_json


class SetupError(RuntimeError):
    def __init__(self,kind,message):
        self.kind=kind
        super().__init__(message)


def activate_pending(settings,attempts=3):
    active=settings.state/'telegram.json'
    pending=settings.state/'telegram.pending.json'
    if active.exists():
        import json
        return json.loads(active.read_text())
    if not pending.exists():
        return None
    import json
    config=json.loads(pending.read_text())
    result=None
    for attempt in range(attempts):
        try:
            response=requests.post('https://api.telegram.org/bot'+config['token']+'/getMe',timeout=(8,12))
            result=response.json()
            break
        except Exception:
            if attempt+1<attempts:time.sleep(2)
    if result is None:
        raise SetupError('network','Токен сохранён локально. Telegram пока не отвечает; повторно вводить токен не нужно.')
    if not result.get('ok'):
        if result.get('error_code') in (401,404):
            pending.unlink(missing_ok=True)
            raise SetupError('invalid','Telegram не принял токен. Проверь, что скопировал его целиком из BotFather.')
        raise SetupError('network','Токен сохранён. Telegram временно отклонил проверку; повторно вводить его не нужно.')
    me=result.get('result') or {}
    username=me.get('username','')
    if not me.get('is_bot') or 'aidj' not in username.casefold():
        raise SetupError('identity','Это другой бот. Ожидаю нового бота с именем вроде aidj_codex_music_bot.')
    config.update(username=username,verified=True)
    atomic_json(active,config)
    pending.unlink(missing_ok=True)
    return config
