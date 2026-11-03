"""Import only YouTube cookies through an echo-disabled local terminal."""
import os
import json
import sys
import tempfile
import termios
import tty
from pathlib import Path
from aidj.config import Settings

DEST = Settings().credentials / 'youtube-cookies.txt'


def show_export_help():
    print('Чтобы cookies не менялись из-за открытой вкладки YouTube:', flush=True)
    print('1. Открой отдельное окно инкогнито и войди в YouTube.', flush=True)
    print('2. В этой же вкладке открой https://www.youtube.com/robots.txt', flush=True)
    print('3. Экспортируй cookies youtube.com именно из этого окна и закрой его.', flush=True)
    print('Обычным окном браузера можно продолжать пользоваться.\n', flush=True)


def read_hidden():
    if not sys.stdin.isatty():
        raise ValueError('TTY_REQUIRED')
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    data = bytearray()
    try:
        tty.setcbreak(fd)
        # Modern terminals delimit clipboard paste automatically. Enter confirms it.
        print('\x1b[?2004h', end='', flush=True)
        print('Вставь cookies и нажми Enter. Ввод скрыт:', flush=True)
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                raise ValueError('INPUT_ENDED')
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise ValueError('INPUT_TOO_LARGE')
            if b'\x04' in chunk:
                raise EOFError()
            normalized = bytes(data).replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            start = normalized.find(b'\x1b[200~')
            if start >= 0:
                start += len(b'\x1b[200~')
                end = normalized.find(b'\x1b[201~', start)
                if end >= 0 and b'\n' in normalized[end + len(b'\x1b[201~'):]:
                    return normalized[start:end].decode('utf-8-sig')
            else:
                # JSON is self-delimiting even in terminals without paste markers.
                try:
                    text = normalized.decode('utf-8-sig').lstrip()
                    if text.startswith(('[', '{')):
                        _, end = json.JSONDecoder().raw_decode(text)
                        if '\n' in text[end:] and not text[end:].strip():
                            return text[:end]
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
    finally:
        print('\x1b[?2004l', end='', flush=True)
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        print('', flush=True)


def youtube_only(text):
    text = text.replace('\x1b[200~', '').replace('\x1b[201~', '')
    if text.lstrip().startswith(('[', '{')):
        cookies = json.loads(text)
        if isinstance(cookies, dict):
            cookies = cookies.get('cookies')
        if not isinstance(cookies, list):
            raise ValueError('EXPECTED_COOKIE_LIST')
        converted = []
        for c in cookies:
            domain = c['domain']
            if c.get('httpOnly'):
                domain = '#HttpOnly_' + domain
            fields = [domain, 'FALSE' if c.get('hostOnly', not c['domain'].startswith('.')) else 'TRUE',
                      c.get('path', '/'), 'TRUE' if c.get('secure') else 'FALSE',
                      str(int(c.get('expirationDate') or 0)), c['name'], c['value']]
            if any(any(ord(ch) < 32 for ch in str(value)) for value in fields):
                raise ValueError('INVALID_COOKIE_VALUE')
            converted.append('\t'.join(str(value) for value in fields))
        text = '\n'.join(converted)
    lines = ['# Netscape HTTP Cookie File', '# YouTube cookies only; imported locally.']
    names = set()
    for line in text.splitlines():
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields = line.split('\t', 6)
        if len(fields) != 7:
            raise ValueError('EXPECTED_NETSCAPE_FORMAT')
        domain = fields[0].removeprefix('#HttpOnly_').lstrip('.').casefold()
        if domain != 'youtube.com' and not domain.endswith('.youtube.com'):
            continue
        if fields[1].upper() not in ('TRUE', 'FALSE') or fields[3].upper() not in ('TRUE', 'FALSE'):
            raise ValueError('INVALID_COOKIE_FLAGS')
        if fields[4] and not fields[4].lstrip('-').isdigit():
            raise ValueError('INVALID_EXPIRY')
        if not fields[5] or any(ord(c) < 32 for c in fields[6]):
            raise ValueError('INVALID_COOKIE_VALUE')
        lines.append(line)
        names.add(fields[5])
    if not names.intersection({'SID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO'}):
        raise ValueError('LOGGED_IN_YOUTUBE_SESSION_NOT_FOUND')
    return '\n'.join(lines) + '\n', len(lines) - 2


def save(text):
    filtered, count = youtube_only(text)
    DEST.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.youtube-cookies-', dir=DEST.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(filtered)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, DEST)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('COOKIES_SAVED', count, '— только YouTube. Можно сообщить в чат: готово.', flush=True)


if __name__ == '__main__':
    try:
        show_export_help()
        save(read_hidden())
    except (KeyboardInterrupt, EOFError):
        print('\nCANCELLED', flush=True)
        raise SystemExit(1)
    except Exception as exc:
        # Never print input, cookie names/values, or exception text derived from input.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else type(exc).__name__
        print('IMPORT_FAILED', code, flush=True)
        raise SystemExit(1)
