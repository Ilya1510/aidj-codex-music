"""Check tracked source paths and secret patterns without printing matched values."""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parent.parent
FORBIDDEN=('*.sqlite','*.sqlite-*','*.db','*.db-*','*.p12','*.pem','*.key','*.jks','*.keystore',
           '*.apk','*credentials*.json','*cookies*.txt','*cookies*.json','*token*.json','*token*.txt')
PATTERNS=(('private-key',rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          ('github-token',rb'\bgh[pousr]_[A-Za-z0-9_]{24,}\b'),
          ('telegram-token',rb'\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b'),
          ('yandex-token',rb'\b(?:y[01]_|t[01]_)[A-Za-z0-9_-]{35,}\b'))


def known_secrets():
    values=[]
    locations=[Path.home()/'.local/share/yandex-music-export/credentials.json',
               Path.home()/'.local/share/yandex-music-export/disk-credentials.json',
               Path.home()/'.local/share/aidj-codex-music/telegram.json',
               Path.home()/'.local/share/aidj-codex-music/android-signing/identity.json']
    for file in locations:
        if not file.is_file():continue
        data=json.loads(file.read_text())
        for key in ('access_token','token','password','pair_code'):
            value=data.get(key)
            if isinstance(value,str) and len(value)>=12:values.append(value.encode())
    return values


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--staged',action='store_true');args=parser.parse_args()
    command=['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'] if args.staged else ['git','ls-files','-z']
    names=subprocess.run(command,cwd=ROOT,check=True,capture_output=True).stdout.split(b'\0')
    secrets=known_secrets();failures=[];count=0;size=0
    for raw in names:
        if not raw:continue
        name=raw.decode();path=ROOT/name
        if path.is_symlink():failures.append((name,'symlink'));continue
        if not path.is_file():continue
        if (Path(name).name=='.env' or Path(name).name.startswith('.env.') and Path(name).name!='.env.example'
            or any(fnmatch.fnmatch(Path(name).name,pattern) for pattern in FORBIDDEN)):
            failures.append((name,'private-or-generated-path'));continue
        content=subprocess.run(['git','show',':'+name],cwd=ROOT,check=True,capture_output=True).stdout if args.staged else path.read_bytes()
        count+=1;size+=len(content)
        if any(secret in content for secret in secrets):failures.append((name,'local-secret-value'))
        for label,pattern in PATTERNS:
            for match in re.finditer(pattern,content):
                # Test fixtures contain clearly named fake credentials, never real values.
                if name.startswith('tests/') and any(marker in match[0] for marker in (b'FAKE',b'TEST',b'DUMMY')):continue
                failures.append((name,label));break
    for name,label in sorted(set(failures)):print('SOURCE_CHECK_FAILED',name,label)
    if failures:return 1
    print('SOURCE_CHECK_OK',count,'files',size,'bytes');return 0


if __name__=='__main__':raise SystemExit(main())
