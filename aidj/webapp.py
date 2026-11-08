"""Role-checked music gateway. Audio is streamed, never buffered in VM RAM."""
from contextlib import contextmanager
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests

from .config import Settings
from .disk import Disk, DiskError
from .playlists import PlaylistError
from .store import connect,meta_get
from .web_auth import AuthError,verify_telegram,create_session,session_access,access,browser_code,redeem_code
from .web_catalog import catalog,mutate,Conflict
from .android_release import version as android_version,connection_url
from .web_transfer import register as register_transfer,current as current_transfer
from .discover import submit as discover_submit, recent as discover_recent, owned_job, public_job, DiscoverError
from .sources import SourceError
from .device_inbox import pending as inbox_pending,acknowledge as inbox_ack,library_revision
from .clips import save_range
from .audio import AudioError
from .review_web import request as review_request

PORT=18782
STATIC=Path(__file__).resolve().parent.parent/'miniapp'
FILES={'/':'index.html','/index.html':'index.html','/app.css':'app.css','/app.mjs':'app.mjs',
       '/native.mjs':'native.mjs','/personal-playlists.mjs':'personal-playlists.mjs','/web-remix.mjs':'web-remix.mjs','/playlist-changes.mjs':'playlist-changes.mjs','/transfer.mjs':'transfer.mjs','/segments.mjs':'segments.mjs','/playback.mjs':'playback.mjs','/dj.mjs':'dj.mjs','/ratings.mjs':'ratings.mjs','/reorder.mjs':'reorder.mjs','/android-connect':'android-connect.html','/android-connect.mjs':'android-connect.mjs',
       '/player.mjs':'player.mjs','/storage.mjs':'storage.mjs','/sw.js':'sw.js',
       '/manifest.webmanifest':'manifest.webmanifest','/icon.svg':'icon.svg','/telegram-web-app.js':'telegram-web-app.js'}
STREAMS=threading.BoundedSemaphore(6)
LINKS={}
LINK_LOCK=threading.Lock()


def client_catalog(db,version,principal=None):
    """Do not send modern pools to old APKs or duplicate them in a new APK."""
    value=catalog(db)
    value['access']=principal or access('admin')
    if value['access']['role']=='listener':
        for playlist in value['playlists']:playlist.pop('diskUrl',None)
        for track in value['tracks']:
            if isinstance(track.get('rating'),dict):track['rating']['mine']=None
    if version and (not re.fullmatch(r'[0-9]{1,6}',version) or int(version)<27):
        value['playlists']=[p for p in value['playlists'] if not p.get('combined')]
        value['combinedSupported']=False
    modern=bool(re.fullmatch(r'[0-9]{1,6}',version or '')) and int(version)>=20
    hidden=('djV3','djV2','dj') if modern else ('djV4',)
    for track in value['tracks']:
        for field in hidden:track.pop(field,None)
        if modern and int(version)<21 and isinstance(track.get('djV4'),dict):
            track['djV4'].pop('anchors',None)
            track['djV4'].pop('acousticAnchors',None)
    return value


def public_url(settings):
    path=settings.state/'miniapp-url.json'
    try:
        value=json.loads(path.read_text()).get('url','')
        parsed=urlsplit(value)
        if (parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
            and parsed.port is None and parsed.path in ('','/') and not parsed.query and not parsed.fragment
            and re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',parsed.hostname)):
            return 'https://'+parsed.hostname
    except (OSError,ValueError):pass
    return ''


@contextmanager
def editing(settings):
    with (settings.state/'worker.lock').open('a') as f:
        deadline=time.monotonic()+15
        while True:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>deadline:raise Conflict('Бот сейчас обрабатывает песню. Повтори чуть позже.')
                time.sleep(.2)
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)


def byte_range(value,size):
    if not value:return 0,size-1,False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
    if not match or not any(match.groups()):raise ValueError('range')
    left,right=match.groups()
    if not left:
        amount=int(right)
        if amount<=0:raise ValueError('range')
        start,end=max(0,size-amount),size-1
    else:start,end=int(left),min(int(right),size-1) if right else size-1
    if start>=size or start>end:raise ValueError('range')
    return start,end,True


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,handler,settings=None):
        self.settings=settings or Settings();self.settings.prepare()
        self.slots=threading.BoundedSemaphore(12)
        super().__init__(address,handler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send_response(self,*args,**kwargs):
        self.response_started=True
        super().send_response(*args,**kwargs)
    def setup(self):
        super().setup();self.connection.settimeout(60)
    def secure_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' http://127.0.0.1:*; worker-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org")
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
    def output(self,status,body,mime='application/json; charset=utf-8',cookie=None):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.secure_headers()
        if status>=400:
            self.close_connection=True
            self.send_header('Connection','close')
        self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store' if mime.startswith('application/json') else 'no-cache')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def read_json(self):
        if 'application/json' not in self.headers.get('Content-Type',''):raise ValueError('JSON required')
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=65536:raise ValueError('body size')
        value=json.loads(self.rfile.read(size))
        if not isinstance(value,dict):raise ValueError('JSON object required')
        return value
    def authenticated(self,db):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return False
        token=cookie.get('aidj_session')
        self.access=session_access(db,token.value if token else '')
        return self.access is not None
    def dispatch(self):
        path=urlsplit(self.path).path
        settings=self.server.settings
        if self.command in ('GET','HEAD') and path=='/api/android-version':
            info=android_version(settings,public_url(settings))
            return self.output(200,info) if info else self.output(503,{'error':'Android-приложение ещё собирается.'})
        match=re.fullmatch(r'/android/releases/([1-9][0-9]{0,8})/AI-DJ\.apk',path)
        if self.command in ('GET','HEAD') and match:
            file=settings.state/'android-release/releases'/match[1]/'AI-DJ.apk'
            if not file.is_file():return self.output(404,{'error':'Версия не найдена.'})
            size=file.stat().st_size
            try:start,end,partial=byte_range(self.headers.get('Range'),size)
            except ValueError:return self.output(416,{'error':'Недопустимый диапазон.'})
            self.send_response(206 if partial else 200);self.secure_headers()
            self.send_header('Content-Type','application/vnd.android.package-archive')
            self.send_header('Content-Disposition','attachment; filename="AI-DJ.apk"')
            self.send_header('Access-Control-Allow-Origin','https://web.telegram.org')
            self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command!='HEAD':
                with file.open('rb') as stream:
                    stream.seek(start);remaining=end-start+1
                    while remaining:
                        chunk=stream.read(min(65536,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
            return
        if self.command in ('GET','HEAD') and path in FILES:
            file=STATIC/FILES[path]
            if not file.is_file():return self.output(404,{'error':'Файл не найден.'})
            mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
            if file.suffix in ('.js','.mjs'):mime='text/javascript; charset=utf-8'
            return self.output(200,file.read_bytes(),mime)
        if path=='/health' and self.command=='GET':return self.output(200,{'ok':True})
        db=connect(settings)
        try:
            if self.command=='POST':
                origin=self.headers.get('Origin')
                allowed={public_url(settings),'http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port)}
                if self.headers.get('X-AIDJ-App')!='1' or (origin and origin not in allowed):
                    return self.output(403,{'error':'Недопустимый запрос.'})
                body=self.read_json()
                if path=='/api/auth':
                    if body.get('loginCode'):
                        token=redeem_code(db,body['loginCode'])
                    else:
                        config=json.loads((settings.state/'telegram.json').read_text())
                        owner=meta_get(db,'owner_id')
                        if not owner:raise AuthError('Владелец ещё не подключил библиотеку.')
                        user_id=verify_telegram(body.get('initData',''),config['token'],owner,allow_listener=True)
                        role='admin' if str(user_id)==str(owner) else 'listener'
                        token=create_session(db,role=role,user_id=user_id)
                    return self.output(200,{'ok':True,'access':session_access(db,token)},cookie='aidj_session='+token+'; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000')
            if not self.authenticated(db):return self.output(401,{'error':'Открой приложение из бота.'})
            if self.access['role']=='listener':
                if path=='/api/device-inbox' and self.command=='POST':return self.output(200,{'revision':library_revision(db),'items':[]})
                if path=='/api/device-ack' and self.command=='POST':return self.output(200,{'ok':True})
                readable=self.command in ('GET','HEAD') and (path in ('/api/catalog','/api/session','/api/library-version') or re.fullmatch(r'/api/audio/[a-f0-9]{24}',path))
                portable=self.command=='POST' and path in ('/api/browser-link','/api/android-connect')
                if not readable and not portable:return self.output(403,{'error':'Режим слушателя: общую библиотеку может изменять только владелец.'})
            if path=='/api/reviews' and self.command=='POST':return self.output(200,review_request(db,body))
            if path=='/api/dj' and self.command=='POST':
                from .dj import enqueue
                return self.output(200,enqueue(db,body))
            if path=='/api/range' and self.command=='POST':return self.output(200,save_range(db,body))
            if path=='/api/device-inbox' and self.command=='POST':return self.output(200,inbox_pending(db,body))
            if path=='/api/device-ack' and self.command=='POST':return self.output(200,inbox_ack(db,body))
            if path=='/api/library-version' and self.command=='GET':return self.output(200,{'revision':library_revision(db)})
            if path=='/api/discover' and self.command=='POST':return self.output(200,discover_submit(db,body))
            if path=='/api/discover' and self.command=='GET':return self.output(200,discover_recent(db))
            match=re.fullmatch(r'/api/discover/([1-9][0-9]{0,12})',path)
            if match and self.command=='GET':return self.output(200,public_job(owned_job(db,int(match[1]))))
            if path=='/api/catalog' and self.command=='GET':return self.output(200,client_catalog(db,self.headers.get('X-AIDJ-Version'),self.access))
            if path=='/api/session' and self.command=='GET':return self.output(200,{'ok':True,'access':self.access})
            if path=='/api/import-register' and self.command=='POST':
                register_transfer(db,body)
                return self.output(200,{'ok':True})
            if path=='/api/import-current' and self.command=='GET':
                descriptor=current_transfer(db)
                return self.output(200,descriptor) if descriptor else self.output(409,{'error':'Сначала открой AI DJ → настройки → «Перенести из Mini App» на этом телефоне.'})
            if path=='/api/browser-link' and self.command=='POST':
                origin=public_url(settings)
                if not origin:raise Conflict('Ссылка пока недоступна.')
                return self.output(200,{'url':origin+'/#login='+browser_code(db,role=self.access['role'],user_id=self.access['userId'])})
            if path=='/api/android-connect' and self.command=='POST':
                origin=public_url(settings)
                if not origin:raise Conflict('Ссылка пока недоступна.')
                return self.output(200,{'url':connection_url(db,origin,role=self.access['role'],user_id=self.access['userId'])})
            if path=='/api/edit' and self.command=='POST':
                from .playlist_combined import ACTIONS as combined_actions
                with editing(settings),tempfile.TemporaryDirectory(prefix='web-edit-',dir=settings.state/'work') as work:
                    mutate(settings,db,None if body.get('action') in combined_actions else Disk(settings),body,Path(work))
                return self.output(200,client_catalog(db,self.headers.get('X-AIDJ-Version')))
            if self.command in ('GET','HEAD') and re.fullmatch(r'/api/audio/[a-f0-9]{24}',path):
                asset=db.execute('SELECT * FROM web_assets WHERE id=?',(path.rsplit('/',1)[1],)).fetchone()
                if not asset:return self.output(404,{'error':'Песня не найдена.'})
                return self.audio(asset,settings)
            return self.output(404,{'error':'Не найдено.'})
        finally:db.close()
    def audio(self,asset,settings):
        size=asset['bytes']
        try:start,end,partial=byte_range(self.headers.get('Range'),size)
        except ValueError:
            self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
        def headers():
            self.send_response(206 if partial else 200);self.secure_headers()
            self.send_header('Content-Type','audio/mp4' if asset['remote_path'].endswith('.m4a') else 'audio/mpeg')
            self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
            self.send_header('Cache-Control','private, no-store')
            self.send_header('ETag','"'+asset['md5']+'"')
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
        if self.command=='HEAD':headers();return
        if not STREAMS.acquire(blocking=False):return self.output(429,{'error':'Дождись текущей загрузки.'})
        try:
            key=(asset['remote_path'],asset['md5'])
            with LINK_LOCK:cached=LINKS.get(key)
            if not cached or cached[0]<time.time():
                disk=Disk(settings)
                if not disk.verify(asset['remote_path'],asset['md5'],size):raise Conflict('Файл на Диске изменился. Обнови библиотеку.')
                r=disk.request('GET','/resources/download',params={'path':asset['remote_path']})
                if r.status_code!=200:raise DiskError('Диск сейчас не отдал песню.')
                cached=(time.time()+180,r.json()['href'])
                with LINK_LOCK:
                    if len(LINKS)>1000:LINKS.clear()
                    LINKS[key]=cached
            with requests.get(cached[1],headers={'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'},stream=True,timeout=(15,60)) as response:
                if response.status_code not in (200,206):raise DiskError('Диск сейчас не отдал аудио.')
                if response.status_code==206:
                    content_range=response.headers.get('Content-Range','')
                    if not content_range.startswith(f'bytes {start}-'):raise DiskError('Неверный ответ аудиосервера.')
                headers()
                skip=start if response.status_code==200 else 0;remaining=end-start+1
                for chunk in response.iter_content(65536):
                    if skip:
                        skipped=min(skip,len(chunk));skip-=skipped;chunk=chunk[skipped:]
                    if not chunk:continue
                    block=chunk[:remaining];self.wfile.write(block);remaining-=len(block)
                    if remaining==0:break
                if remaining:self.close_connection=True
        finally:STREAMS.release()
    def handle_request(self):
        self.response_started=False
        def fail(status,message):
            if self.response_started:self.close_connection=True
            else:self.output(status,{'error':message})
        try:self.dispatch()
        except (BrokenPipeError,ConnectionResetError,socket.timeout):self.close_connection=True
        except AuthError as exc:fail(403,str(exc))
        except Conflict as exc:fail(409,str(exc))
        except DiskError as exc:fail(503 if urlsplit(self.path).path=='/api/edit' else 400,str(exc))
        except (PlaylistError,SourceError,DiscoverError,AudioError) as exc:fail(400,str(exc))
        except (ValueError,TypeError,KeyError):fail(400,'Некорректный запрос.')
        except Exception as exc:
            print('WEB_REQUEST_FAILED',type(exc).__name__,flush=True)
            fail(503,'Сервис временно недоступен. Попробуй ещё раз.')
    do_GET=handle_request
    do_HEAD=handle_request
    do_POST=handle_request


def main():
    server=Server(('127.0.0.1',PORT),Handler)
    print('MUSIC_WEB_READY',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
