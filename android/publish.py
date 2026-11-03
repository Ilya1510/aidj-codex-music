"""Publish a verified APK under an immutable version URL."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from aidj.config import Settings,atomic_json


def main():
    settings=Settings();staged=settings.state/'android-build'
    metadata=json.loads((staged/'version.json').read_text());apk=staged/'AI-DJ.apk'
    with apk.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
    if metadata['sha256']!=actual or metadata['bytes']!=apk.stat().st_size:raise RuntimeError('STAGED_APK_CHANGED')
    private=[]
    for filename in ['credentials.json','disk-credentials.json']:
        value=settings.secret(filename).get('access_token','')
        if value:private.append(value.encode())
    value=json.loads((settings.state/'telegram.json').read_text()).get('token','')
    if value:private.append(value.encode())
    with zipfile.ZipFile(apk) as archive:
        required={'AndroidManifest.xml','classes.dex','assets/web/native.mjs','assets/web/app.mjs'}
        if not required.issubset(archive.namelist()):raise RuntimeError('INCOMPLETE_APK')
        for name in archive.namelist():
            data=archive.read(name)
            if any(secret in data for secret in private):raise RuntimeError('PRIVATE_DATA_IN_APK')
    root=settings.state/'android-release';target=root/'releases'/str(metadata['versionCode'])
    if (root/'version.json').exists():
        old=json.loads((root/'version.json').read_text())
        if old['signerSha256']!=metadata['signerSha256']:raise RuntimeError('SIGNING_IDENTITY_CHANGED')
        if metadata['versionCode']<old['versionCode']:raise RuntimeError('VERSION_DOWNGRADE')
    target.mkdir(parents=True,exist_ok=True)
    final=target/'AI-DJ.apk'
    if final.exists():
        with final.open('rb') as f:previous=hashlib.file_digest(f,'sha256').hexdigest()
        if previous!=actual:raise RuntimeError('VERSION_ALREADY_PUBLISHED')
    else:
        temporary=target/'AI-DJ.apk.tmp';shutil.copyfile(apk,temporary);temporary.chmod(0o644);temporary.replace(final)
    atomic_json(root/'version.json',metadata)
    print('APK_PUBLISHED',metadata['versionName'],metadata['versionCode'],metadata['bytes'],flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('APK_PUBLICATION_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else '',flush=True)
        raise SystemExit(1)
