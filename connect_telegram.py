"""Save a Telegram bot token using a normal hidden single-line prompt."""
import getpass
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

import requests
from aidj.config import Settings,atomic_json
from aidj.bootstrap import activate_pending,SetupError


def main():
    settings=Settings();settings.prepare()
    active=settings.state/'telegram.json'
    pending=settings.state/'telegram.pending.json'
    if not active.exists() and not pending.exists():
        token=getpass.getpass('Токен нового бота от BotFather (ввод скрыт): ').strip()
        if not re.fullmatch(r'\d{5,}:[A-Za-z0-9_-]{25,}',token):
            print('Не похоже на токен Telegram. Значение не сохранено.')
            return 1
        atomic_json(pending,{'token':token,'pair_code':secrets.token_urlsafe(18),'verified':False})
    print('Проверяю подключение…',flush=True)
    try:
        config=activate_pending(settings)
    except SetupError as exc:
        print(str(exc))
        return 1
    print('BOT_CONNECTED @'+config['username'])
    if config.get('pair_code'):
        print('Открой эту одноразовую ссылку, чтобы закрепить бота за собой:')
        print('https://t.me/'+config['username']+'?start='+config['pair_code'])
    else:
        print('Бот уже закреплён за тобой: https://t.me/'+config['username'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
