"""Check native Java GCM against the browser-compatible AES-GCM wire format."""
import base64
import hashlib
from pathlib import Path
import subprocess
import tempfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main():
    root=Path(__file__).resolve().parents[1]
    jdk=Path.home()/'.local/share/aidj-codex-music/android-tools/jdk17/bin'
    token='test-phone-import-'.ljust(43,'x')
    nonce=bytes(range(12));route='/file/'+'a'*32;plain=b'cached audio bytes\0'*100
    encode=lambda value:base64.b64encode(value).decode()
    aes=AESGCM(hashlib.sha256(token.encode()).digest())
    with tempfile.TemporaryDirectory(prefix='aidj-crypto-') as directory:
        subprocess.run([str(jdk/'javac'),'--release','8','-d',directory,str(root/'android/src/com/aidj/music/TransferCrypto.java'),str(root/'tests/TransferCryptoProbe.java')],check=True,capture_output=True)
        def java(mode,data,path=route):
            return subprocess.run([str(jdk/'java'),'-Xmx64m','-XX:ActiveProcessorCount=2','-cp',directory,'com.aidj.music.TransferCryptoProbe',mode,base64.urlsafe_b64encode(nonce).decode().rstrip('='),path,encode(data)],capture_output=True)
        encrypted=aes.encrypt(nonce,plain,route.encode())
        response=java('decrypt',encrypted);assert response.returncode==0 and base64.b64decode(response.stdout)==plain
        response=java('encrypt',plain);assert response.returncode==0 and aes.decrypt(nonce,base64.b64decode(response.stdout),route.encode())==plain
        assert java('decrypt',encrypted,'/manifest').returncode!=0
        assert java('decrypt',encrypted[:-1]+bytes([encrypted[-1]^1])).returncode!=0
    print('NATIVE_CRYPTO_INTEROP_PASSED both_directions wrong_route tamper_rejected',flush=True)


if __name__=='__main__':main()
