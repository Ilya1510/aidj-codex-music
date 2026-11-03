"""Fetch only QA browser libraries from Ubuntu; do not change host packages."""
import concurrent.futures
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

import requests

PACKAGES='libnspr4 libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libatspi2.0-0t64 libdrm2 libdrm-common'.split()
ROOT=Path.home()/'.local/share/aidj-codex-music/browser-libs'


def url(package):
    r=requests.get(f'https://packages.ubuntu.com/noble/amd64/{package}/download',timeout=30)
    r.raise_for_status()
    links=re.findall(r'href="(https?://[^\"]+\.deb)"',r.text)
    if not links and package=='libdrm-common':
        r=requests.get('https://packages.ubuntu.com/noble/all/libdrm-common/download',timeout=30)
        links=re.findall(r'href="(https?://[^\"]+\.deb)"',r.text)
    if not links:raise RuntimeError('NO_DOWNLOAD_'+package)
    return package,'https://archive.ubuntu.com'+urlsplit(links[0]).path


def main():
    ROOT.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        urls=list(pool.map(url,sys.argv[1:] or PACKAGES))
    for package,href in urls:
        target=ROOT/href.rsplit('/',1)[1]
        if not target.exists():
            with requests.get(href,stream=True,timeout=45) as r:
                r.raise_for_status();size=0
                with target.open('wb') as out:
                    for chunk in r.iter_content(131072):
                        size+=len(chunk)
                        if size>32*1024**2:raise RuntimeError('PACKAGE_TOO_BIG')
                        out.write(chunk)
        subprocess.run(['dpkg-deb','-x',str(target),str(ROOT/'root')],check=True,capture_output=True)
        print('QA_LIBRARY_READY',package,flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('QA_LIBRARIES_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else '',flush=True)
        raise SystemExit(1)
