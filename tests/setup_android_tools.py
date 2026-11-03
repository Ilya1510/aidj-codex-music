"""Install a minimal, checksum-verified toolchain without Gradle or an emulator."""
import hashlib
from pathlib import Path
import shutil
import tarfile
import xml.etree.ElementTree as ET
import zipfile

import requests

ROOT=Path.home()/'.local/share/aidj-codex-music/android-tools'


def download(url,target,size,checksum,algorithm):
    if target.exists():
        h=hashlib.new(algorithm)
        with target.open('rb') as f:
            for chunk in iter(lambda:f.read(262144),b''):h.update(chunk)
        if h.hexdigest()==checksum:return
        target.unlink()
    if shutil.disk_usage(ROOT).free<2*1024**3:raise RuntimeError('LOW_DISK')
    h=hashlib.new(algorithm);total=0
    with requests.get(url,stream=True,timeout=(15,90)) as r:
        r.raise_for_status()
        with target.open('wb') as f:
            for chunk in r.iter_content(262144):
                total+=len(chunk)
                if total>size+1048576:raise RuntimeError('UNEXPECTED_DOWNLOAD_SIZE')
                h.update(chunk);f.write(chunk)
    if total!=size or h.hexdigest()!=checksum:raise RuntimeError('CHECKSUM_MISMATCH')
    print('DOWNLOADED',target.name,total,flush=True)


def main():
    ROOT.mkdir(exist_ok=True)
    r=requests.get('https://api.adoptium.net/v3/assets/latest/17/hotspot',params={'architecture':'x64','image_type':'jdk','os':'linux','vendor':'eclipse'},timeout=30)
    r.raise_for_status();package=r.json()[0]['binary']['package']
    archive=ROOT/'jdk17.tar.gz'
    if not (ROOT/'jdk17/bin/java').exists():
        download(package['link'],archive,package['size'],package['checksum'],'sha256')
        with tarfile.open(archive) as f:
            top=f.getnames()[0].split('/')[0];f.extractall(ROOT,filter='data')
        (ROOT/top).rename(ROOT/'jdk17');archive.unlink()
    print('JDK_READY',flush=True)
    r=requests.get('https://dl.google.com/android/repository/repository2-1.xml',timeout=40);r.raise_for_status()
    root=ET.fromstring(r.content)
    for package,name in [('platforms;android-35','platform35'),('build-tools;35.0.0','build35')]:
        target=ROOT/name
        if target.exists():print('SDK_READY',name,flush=True);continue
        remote=next(x for x in root.iter() if x.tag.rsplit('}',1)[-1]=='remotePackage' and x.get('path')==package)
        archives=next(x for x in remote if x.tag.rsplit('}',1)[-1]=='archives')
        selected=None
        for item in archives:
            values={x.tag.rsplit('}',1)[-1]:x for x in item}
            if 'host-os' not in values or values['host-os'].text=='linux':selected=values['complete'];break
        fields={x.tag.rsplit('}',1)[-1]:x for x in selected}
        filename=fields['url'].text;archive=ROOT/(name+'.zip')
        checksum=fields['checksum'].text;algorithm=fields['checksum'].get('type','sha1')
        download('https://dl.google.com/android/repository/'+filename,archive,int(fields['size'].text),checksum,algorithm)
        target.mkdir()
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                p=(target/entry.filename).resolve()
                if not p.is_relative_to(target.resolve()):raise RuntimeError('BAD_ZIP_PATH')
            z.extractall(target)
        for file in target.rglob('*'):
            if file.is_file() and (file.name in ['aapt2','aapt','zipalign','apksigner','d8'] or file.suffix=='.so'):
                file.chmod(0o755)
        archive.unlink();print('SDK_READY',name,flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('ANDROID_TOOLS_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else '',flush=True)
        raise SystemExit(1)
