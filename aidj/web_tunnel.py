"""HTTPS transport for the owner-authenticated music gateway."""
import hashlib
import re
import signal
import subprocess
import time
import os
from pathlib import Path

from .config import Settings,atomic_json
from .webapp import PORT

BINARY=Settings().state/'tools/cloudflared'
DIGEST='e1bc0cf084fd9e590554fe769a86b1db46701c3f847a43d902f7b974a4e16298'


def main():
    settings=Settings();settings.prepare()
    owned=settings.state/'tools/cloudflared'
    binary=Path(os.environ['AIDJ_CLOUDFLARED']).expanduser() if os.environ.get('AIDJ_CLOUDFLARED') else (owned if owned.is_file() else BINARY)
    digest=hashlib.sha256()
    with binary.open('rb') as f:
        for chunk in iter(lambda:f.read(262144),b''):digest.update(chunk)
    if digest.hexdigest()!=DIGEST:raise RuntimeError('TUNNEL_BINARY_CHANGED')
    url_file=settings.state/'miniapp-url.json'
    stopped=False;process=None
    def stop(*args):
        nonlocal stopped
        stopped=True
        if process and process.poll() is None:process.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    while not stopped:
        process=subprocess.Popen([str(binary),'tunnel','--no-autoupdate','--protocol','http2',
            '--metrics','127.0.0.1:18783','--url',f'http://127.0.0.1:{PORT}'],
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            for line in process.stdout:
                found=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
                if found:
                    atomic_json(url_file,{'url':found[0],'started':time.time()})
                    print('MUSIC_HTTPS_READY',flush=True)
                if stopped:break
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            process.stdout.close()
        if not stopped:
            print('MUSIC_HTTPS_RECONNECTING',flush=True)
            for _ in range(10):
                if stopped:break
                time.sleep(1)


if __name__=='__main__':main()
