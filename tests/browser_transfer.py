"""Exercise real HTTPS-to-loopback encrypted cached-file transfer in Chromium."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright

from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest
from aidj.webapp import public_url


def main():
    settings=Settings();db=connect(settings);session=create_session(db);origin=public_url(settings)
    key='test-phone-import-'.ljust(43,'x');aes=AESGCM(hashlib.sha256(key.encode()).digest())
    body=b'CACHED_PHONE_AUDIO_FRAME'*48000
    checksum=hashlib.md5(body).hexdigest()
    track={'id':'1'*24,'md5':checksum,'bytes':len(body),'title':'Cached migration test','artist':'QA','duration':120,'format':'mp3'}
    data={'tracks':[track],'playlists':[{'id':1,'name':'Transfer QA','trackIds':[track['id']],'revision':'test','diskUrl':origin}]}
    received=[];requests_seen=[]
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def cors(self):
            self.send_header('Access-Control-Allow-Origin',origin)
            self.send_header('Access-Control-Allow-Methods','POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers','Content-Type, X-AIDJ-IV')
            self.send_header('Access-Control-Expose-Headers','X-AIDJ-IV')
            self.send_header('Access-Control-Allow-Private-Network','true')
        def do_OPTIONS(self):self.send_response(204);self.cors();self.end_headers()
        def do_POST(self):
            assert self.headers.get('Origin')==origin
            iv=self.headers['X-AIDJ-IV'];nonce=base64.urlsafe_b64decode(iv+'='*(-len(iv)%4))
            encrypted=self.rfile.read(int(self.headers['Content-Length']))
            raw=aes.decrypt(nonce,encrypted,self.path.encode())
            requests_seen.append(self.path)
            if self.path=='/manifest':reply={'missing':[checksum],'existing':[]}
            elif self.path=='/finish':reply={'ok':True}
            else:
                assert raw==body and self.path=='/file/'+checksum
                received.append(hashlib.md5(raw).hexdigest());reply={'ok':True,'md5':checksum}
            nonce=os.urandom(12);encoded=base64.urlsafe_b64encode(nonce).decode().rstrip('=')
            result=aes.encrypt(nonce,json.dumps(reply).encode(),('reply:'+self.path).encode())
            self.send_response(200);self.cors();self.send_header('X-AIDJ-IV',encoded);self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    stage='launch'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':412,'height':780},is_mobile=True,has_touch=True,service_workers='block')
            for permission in ['local-network-access','loopback-network']:
                try:context.grant_permissions([permission],origin=origin)
                except Exception:pass
            context.add_cookies([{'name':'aidj_session','value':session,'url':origin,'secure':True,'httpOnly':True,'sameSite':'Strict'}])
            page=context.new_page();errors=[]
            page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/api/catalog',lambda route:route.fulfill(json=data))
            descriptor={'port':server.server_port,'token':key}
            page.route('**/api/import-current',lambda route:route.fulfill(json=descriptor))
            stage='load';page.goto(origin,wait_until='networkidle',timeout=30000);page.get_by_role('heading',name='Моя музыка',exact=True).wait_for()
            stage='cache';page.evaluate('''async track=>{const s=await import('/storage.mjs');await s.save(track,new Blob(['CACHED_PHONE_AUDIO_FRAME'.repeat(48000)]));}''',track)
            stage='start';page.locator('#settings').click();page.get_by_role('button',name='Перенести скачанное в APK',exact=True).click()
            page.get_by_role('button',name='Перенести песни · 1',exact=True).click()
            page.wait_for_timeout(500)
            print('TRANSFER_EARLY_STATUS',page.locator('#toast').inner_text(),page.locator('button[disabled]').count(),flush=True)
            stage='copy'
            try:
                page.wait_for_function("()=>!document.querySelector('button[aria-label=\"Перенести песни · 1\"]').disabled",timeout=30000)
                assert page.get_by_text('Готово. В APK: 1 песня.',exact=True).count()==1
            except Exception:
                print('PAGE_ERRORS',str(errors)[-2500:],flush=True)
                print('PAGE_TEXT',page.locator('body').inner_text()[-2000:],flush=True)
                print('LOCAL_REQUESTS',requests_seen,flush=True)
                raise
            assert received==[checksum] and requests_seen==['/manifest','/file/'+checksum,'/finish']
            assert page.evaluate('''async md5=>Boolean(await(await import('/storage.mjs')).blob(md5))''',checksum)
            browser.close()
        print('BROWSER_TRANSFER_PASSED HTTPS_to_loopback encrypted cached_bytes_retained',len(body),flush=True)
    except Exception as exc:
        print('BROWSER_TRANSFER_FAILED',stage,type(exc).__name__,str(exc).replace(session,'[SESSION]')[:1800],flush=True)
        print('LOCAL_REQUESTS',requests_seen,flush=True);raise SystemExit(1)
    finally:
        server.shutdown();server.server_close();thread.join();db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(session),));db.commit()


if __name__=='__main__':main()
