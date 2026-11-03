"""Install checksum-verified Node and cloudflared for Linux x86_64."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from aidj.config import Settings
from aidj.web_tunnel import DIGEST

NODE='24.18.0'
CLOUDFLARED='2026.10.5'


def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def fetch(url,path,expected,limit):
    if shutil.disk_usage(path.parent).free<512*1024**2:raise RuntimeError('LOW_DISK')
    size=0;digest=hashlib.sha256()
    try:
        with urllib.request.urlopen(url,timeout=60) as response,path.open('wb') as output:
            while chunk:=response.read(131072):
                size+=len(chunk)
                if size>limit:raise RuntimeError('DOWNLOAD_TOO_LARGE')
                digest.update(chunk);output.write(chunk)
        if digest.hexdigest()!=expected:raise RuntimeError('CHECKSUM_MISMATCH')
    except BaseException:path.unlink(missing_ok=True);raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','amd64'):raise RuntimeError('Linux x86_64 required')
    settings=Settings();settings.prepare();tools=settings.state/'tools';tools.mkdir(exist_ok=True,mode=0o700)
    local=Path.home()/'.local/bin/node';node=local if local.is_file() else Path(shutil.which('node') or '/missing-node')
    valid=False
    if node.is_file():
        result=subprocess.run([str(node),'--version'],capture_output=True,text=True,timeout=10)
        try:valid=result.returncode==0 and int(result.stdout.strip().lstrip('v').split('.')[0])>=20
        except ValueError:pass
    if not valid and not args.check:
        filename=f'node-v{NODE}-linux-x64.tar.xz';base=f'https://nodejs.org/dist/v{NODE}/'
        with urllib.request.urlopen(base+'SHASUMS256.txt',timeout=30) as response:sums=response.read(65536).decode()
        expected=next(line.split()[0] for line in sums.splitlines() if line.split()[-1]==filename)
        with tempfile.TemporaryDirectory(prefix='runtime-tools-',dir=settings.state/'work') as work:
            archive=Path(work)/filename;fetch(base+filename,archive,expected,80*1024**2)
            local.parent.mkdir(parents=True,exist_ok=True)
            with tarfile.open(archive) as package,package.extractfile(f'node-v{NODE}-linux-x64/bin/node') as stream:
                temporary=local.with_suffix('.installing')
                with temporary.open('wb') as output:shutil.copyfileobj(stream,output,131072)
                temporary.chmod(0o755);temporary.replace(local)
        valid=True
    binary=tools/'cloudflared';tunnel_ok=binary.is_file() and checksum(binary)==DIGEST
    if not tunnel_ok and not args.check:
        temporary=tools/'cloudflared.installing'
        fetch(f'https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED}/cloudflared-linux-amd64',temporary,DIGEST,80*1024**2)
        temporary.chmod(0o755);temporary.replace(binary);tunnel_ok=True
    print('Node:', 'ready' if valid else 'missing or older than 20')
    print('cloudflared:', 'ready' if tunnel_ok else 'missing or checksum mismatch')
    return 0 if valid and tunnel_ok else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print('RUNTIME_TOOLS_FAILED',type(error).__name__,flush=True);raise SystemExit(1)
