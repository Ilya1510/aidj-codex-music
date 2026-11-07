"""Run the actual Android streaming code on the JVM against a local HTTP server."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import threading

import pytest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'android/src/com/aidj/music'
BODY=bytes(range(256))*2048
DIGEST=hashlib.md5(BODY).hexdigest()


@pytest.fixture(scope='module')
def jvm(tmp_path_factory):
    local=Path.home()/'.local/share/aidj-codex-music/android-tools/jdk17/bin'
    javac=str(local/'javac') if (local/'javac').exists() else shutil.which('javac')
    java=str(local/'java') if (local/'java').exists() else shutil.which('java')
    if not javac or not java:pytest.skip('JDK required for native streaming integration')
    output=tmp_path_factory.mktemp('download-jvm')
    subprocess.run([javac,'-J-Xmx128m','-J-XX:ActiveProcessorCount=2','--release','8','-d',str(output),str(SOURCE/'DownloadTransfer.java'),str(SOURCE/'DownloadPolicy.java'),str(ROOT/'tests/DownloadTransferProbe.java')],check=True,timeout=30,capture_output=True)
    def run(url,file=None,stop=0):
        args=['policy'] if url=='policy' else [url,str(file),str(len(BODY)),DIGEST,str(stop)]
        result=subprocess.run([java,'-Xmx64m','-XX:ActiveProcessorCount=2','-cp',str(output),'com.aidj.music.DownloadTransferProbe',*args],check=True,timeout=15,capture_output=True,text=True)
        return result.stdout.strip()
    return run


@pytest.fixture
def server():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            calls.append({'path':self.path,'range':self.headers.get('Range'),'ifRange':self.headers.get('If-Range'),'cookie':self.headers.get('Cookie')})
            mode=self.path.strip('/')
            if mode in ('401','403','404','429','503'):
                self.send_response(int(mode));self.send_header('Content-Length','0');self.end_headers();return
            if mode=='redirect':
                self.send_response(302);self.send_header('Location','/should-not-receive-cookie');self.send_header('Content-Length','0');self.end_headers();return
            start=int(self.headers.get('Range','bytes=0-')[6:-1])
            ranged=self.headers.get('Range') is not None and mode!='ignore-range'
            if not ranged:start=0
            self.send_response(206 if ranged else 200)
            self.send_header('ETag','"'+('0'*32 if mode=='etag-changed' else DIGEST)+'"')
            self.send_header('Content-Length',str(len(BODY)-start+(1 if mode=='wrong-size' else 0)))
            if ranged:self.send_header('Content-Range',f'bytes {start+(1 if mode=="wrong-range" else 0)}-{len(BODY)-1}/{len(BODY)}')
            self.end_headers()
            data=bytes(len(BODY)-start) if mode=='corrupt' else BODY[start:]
            try:
                if mode=='drop':
                    self.wfile.write(data[:131072]);self.wfile.flush();self.connection.shutdown(socket.SHUT_RDWR);self.connection.close()
                else:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{http.server_port}',calls
    finally:http.shutdown();http.server_close();thread.join()


def test_native_download_resumes_after_a_dropped_connection_in_a_new_process(jvm,server,tmp_path):
    base,calls=server;part=tmp_path/'song.part'
    assert jvm(base+'/drop',part)=='RETRY'
    offset=part.stat().st_size;assert 0<offset<len(BODY)
    assert jvm(base+'/good',part)=='VERIFIED'
    assert calls[-1]['range']==f'bytes={offset}-' and calls[-1]['ifRange']=='"'+DIGEST+'"'
    assert calls[-1]['cookie']=='aidj_session=test-only'
    assert part.read_bytes()==BODY
    # A fully verified part can be committed after a process restart without another HTTP request.
    count=len(calls);assert jvm(base+'/good',part)=='VERIFIED' and len(calls)==count


def test_native_cancel_retains_partial_bytes_and_resumes_them(jvm,server,tmp_path):
    base,calls=server;part=tmp_path/'song.part'
    assert jvm(base+'/good',part,65536)=='INTERRUPTED'
    offset=part.stat().st_size;assert 0<offset<len(BODY)
    assert jvm(base+'/good',part)=='VERIFIED'
    assert calls[-1]['range']==f'bytes={offset}-'
    assert part.read_bytes()==BODY


def test_native_resume_restarts_safely_if_server_ignores_range(jvm,server,tmp_path):
    base,calls=server;part=tmp_path/'song.part';part.write_bytes(BODY[:1000])
    assert jvm(base+'/ignore-range',part)=='VERIFIED'
    assert part.read_bytes()==BODY and calls[-1]['range']=='bytes=1000-'


@pytest.mark.parametrize('mode,code',[('wrong-range',409),('etag-changed',409),('wrong-size',409),('401',401),('403',403),('404',404),('429',429),('503',503)])
def test_native_invalid_or_unavailable_responses_never_append_to_valid_partial_bytes(jvm,server,tmp_path,mode,code):
    base,calls=server;part=tmp_path/'song.part';part.write_bytes(BODY[:1000])
    assert jvm(base+'/'+mode,part)==f'FAILURE:{code}'
    assert part.read_bytes()==BODY[:1000]


def test_native_downloader_does_not_forward_authentication_to_redirects(jvm,server,tmp_path):
    base,calls=server;part=tmp_path/'song.part'
    assert jvm(base+'/redirect',part)=='FAILURE:302'
    assert [c['path'] for c in calls]==['/redirect'] and not part.exists()


def test_native_corrupt_full_or_partial_files_never_become_ready(jvm,server,tmp_path):
    base,calls=server;part=tmp_path/'song.part'
    assert jvm(base+'/corrupt',part)=='FAILURE:422' and not part.exists()
    part.write_bytes(bytes(len(BODY)))
    assert jvm(base+'/good',part)=='VERIFIED' and part.read_bytes()==BODY
    assert calls[-1]['range'] is None


def test_native_retry_policy_is_bounded_and_keeps_transient_errors_recoverable(jvm):
    assert jvm('policy')=='POLICY_OK'


def test_android_database_upgrade_preserves_cached_files_queue_and_connection(tmp_path):
    # Execute the exact DDL used by the Android helper against an existing v1 database.
    source=(SOURCE/'State.java').read_text()
    upgrade=source.split('public void onUpgrade',1)[1].split('synchronized String read',1)[0]
    statements=re.findall(r'db\.execSQL\("([^"]+)"\)',upgrade)
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
    db.execute("CREATE TABLE downloads(md5 TEXT PRIMARY KEY,track TEXT NOT NULL,path TEXT NOT NULL DEFAULT '',request_id INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '')")
    file=tmp_path/'already-downloaded.mp3';file.write_bytes(b'cached music')
    db.execute('INSERT INTO meta VALUES (?,?)',('session','test-only'))
    for index,status in enumerate(['ready','pending','active','failed']):db.execute('INSERT INTO downloads VALUES (?,?,?,?,?,?)',(str(index),'{}',str(file),123 if status=='active' else 0,status,''))
    for sql in statements:db.execute(sql)
    rows=db.execute('SELECT status,path,attempts,retry_at FROM downloads ORDER BY md5').fetchall()
    assert [r[0] for r in rows]==['ready','pending','active','failed']
    assert all(r[1]==str(file) and r[2:]==(0,0) for r in rows)
    assert db.execute('SELECT v FROM meta WHERE k=\'session\'').fetchone()[0]=='test-only'
    assert file.read_bytes()==b'cached music'


def test_android_queue_selection_resumes_active_then_due_retries_and_keeps_fifo():
    source=(SOURCE/'State.java').read_text().split('synchronized JSONObject next(long now)',1)[1].split('synchronized JSONArray list',1)[0]
    sql=re.search(r'rawQuery\("([^"\n]+)"',source).group(1)
    db=sqlite3.connect(':memory:')
    db.execute("CREATE TABLE downloads(track TEXT,path TEXT,request_id INTEGER,status TEXT,error TEXT,attempts INTEGER,retry_at INTEGER)")
    for label,status,retry in [('pending-first','pending',0),('future','retry',2000),('due','retry',500),('active','active',0),('pending-second','pending',0)]:
        db.execute("INSERT INTO downloads VALUES (?,'',0,?,'',0,?)",(label,status,retry))
    order=[]
    while row:=db.execute(sql,(1000,)).fetchone():
        order.append(row[0]);db.execute("UPDATE downloads SET status='ready' WHERE track=?",(row[0],))
    assert order==['active','due','pending-first','pending-second']
    assert db.execute(sql,(2000,)).fetchone()[0]=='future'
