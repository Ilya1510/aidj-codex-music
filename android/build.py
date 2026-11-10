"""Build and sign the personal APK with a persistent private signing identity."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from aidj.config import Settings,atomic_json

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parent
settings=Settings();settings.prepare()
TOOLS=settings.state/'android-tools'
JAVA=TOOLS/'jdk17/bin/java'
JAVAC=TOOLS/'jdk17/bin/javac'
BUILD=TOOLS/'build35/android-15'
PLATFORM=next((TOOLS/'platform35').rglob('android.jar'))
OUTPUT=settings.state/'android-build'


def run(args,task_env=None):
    r=subprocess.run([str(x) for x in args],env=task_env,capture_output=True,text=True,timeout=240)
    if r.returncode:
        print((r.stdout+'\n'+r.stderr)[-6500:],flush=True)
        raise RuntimeError('BUILD_STEP_FAILED')
    if r.stderr and 'javac' in str(args[0]):print(r.stderr[-1000:],flush=True)
    return r.stdout


def main():
    # This is a small Java/WebView shell (no SDK/model downloads here).
    # Keep ample scratch space without requiring 2GiB for a sub-megabyte APK.
    if shutil.disk_usage(ROOT).free<512*1024**2:raise RuntimeError('LOW_DISK')
    OUTPUT.mkdir(exist_ok=True)
    for name in ['generated','classes','dex','assets']:
        path=OUTPUT/name
        if path.exists():shutil.rmtree(path)
        path.mkdir()
    shutil.copytree(PROJECT/'miniapp',OUTPUT/'assets/web')
    server=json.loads((settings.state/'miniapp-url.json').read_text())['url']
    generated=OUTPUT/'generated/com/aidj/music';generated.mkdir(parents=True)
    manifest=(ROOT/'AndroidManifest.xml').read_text()
    version=int(re.search(r'android:versionCode="(\d+)"',manifest)[1]);name=re.search(r'android:versionName="([^"]+)"',manifest)[1]
    (generated/'BuildConfig.java').write_text('package com.aidj.music; final class BuildConfig { static final String SERVER='+json.dumps(server)+'; static final int VERSION_CODE='+str(version)+'; }\n')
    print('COMPILE_RESOURCES',flush=True)
    run([BUILD/'aapt2','compile','--dir',ROOT/'res','-o',OUTPUT/'resources.zip'])
    run([BUILD/'aapt2','link','-o',OUTPUT/'unsigned.apk','--manifest',ROOT/'AndroidManifest.xml','-I',PLATFORM,'--java',OUTPUT/'generated','-A',OUTPUT/'assets',OUTPUT/'resources.zip'])
    sources=[*list((ROOT/'src').rglob('*.java')),*list((OUTPUT/'generated').rglob('*.java'))]
    print('COMPILE_JAVA',len(sources),flush=True)
    run([JAVAC,'-J-Xmx384m','-J-XX:ActiveProcessorCount=2','--release','8','-classpath',PLATFORM,'-encoding','UTF-8','-d',OUTPUT/'classes',*sources])
    print('BUILD_DEX',flush=True)
    run([JAVA,'-Xmx384m','-XX:ActiveProcessorCount=2','-cp',BUILD/'lib/d8.jar','com.android.tools.r8.D8','--min-api','26','--lib',PLATFORM,'--output',OUTPUT/'dex',*list((OUTPUT/'classes').rglob('*.class'))])
    with zipfile.ZipFile(OUTPUT/'unsigned.apk','a',compression=zipfile.ZIP_DEFLATED) as archive:
        for file in (OUTPUT/'dex').glob('*.dex'):archive.write(file,file.name)
    run([BUILD/'zipalign','-P','16','-f','4',OUTPUT/'unsigned.apk',OUTPUT/'aligned.apk'])
    signing=settings.state/'android-signing';signing.mkdir(exist_ok=True,mode=0o700)
    password_file=signing/'identity.json';keystore=signing/'aidj.p12'
    if not password_file.exists():
        if keystore.exists():raise RuntimeError('SIGNING_IDENTITY_MISSING')
        atomic_json(password_file,{'password':secrets.token_urlsafe(48)})
    password=json.loads(password_file.read_text())['password']
    task_env=dict(os.environ);task_env['AIDJ_KEYSTORE_PASSWORD']=password
    if not keystore.exists():
        run([TOOLS/'jdk17/bin/keytool','-genkeypair','-keystore',keystore,'-storetype','PKCS12','-alias','aidj','-keyalg','RSA','-keysize','3072','-validity','10000','-dname','CN=AI DJ','-storepass:env','AIDJ_KEYSTORE_PASSWORD','-keypass:env','AIDJ_KEYSTORE_PASSWORD'],task_env)
        keystore.chmod(0o600)
    staged=OUTPUT/'AI-DJ.apk'
    run([JAVA,'-Xmx128m','-jar',BUILD/'lib/apksigner.jar','sign','--ks',keystore,'--ks-pass','env:AIDJ_KEYSTORE_PASSWORD','--ks-key-alias','aidj','--out',staged,OUTPUT/'aligned.apk'],task_env)
    info=run([JAVA,'-Xmx128m','-jar',BUILD/'lib/apksigner.jar','verify','--verbose','--print-certs',staged])
    fingerprint=re.search(r'certificate SHA-256 digest: ([a-f0-9]+)',info)[1]
    old=settings.state/'android-release/version.json'
    if old.exists() and json.loads(old.read_text())['signerSha256']!=fingerprint:raise RuntimeError('SIGNING_KEY_CHANGED')
    result={'versionCode':version,'versionName':name,'sha256':hashlib.file_digest(staged.open('rb'),'sha256').hexdigest(),'bytes':staged.stat().st_size,'signerSha256':fingerprint}
    atomic_json(OUTPUT/'version.json',result)
    print('SIGNED_APK_READY',json.dumps(result),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('ANDROID_BUILD_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else '',flush=True)
        raise SystemExit(1)
