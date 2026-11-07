"""Exercise the real queue worker with lifecycle fixtures and real HTTP transfers."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import pytest

ROOT=Path(__file__).resolve().parents[1]


def test_native_queue_survives_retry_pause_job_restart_and_network_restoration(tmp_path):
    tools=Path.home()/'.local/share/aidj-codex-music/android-tools'
    javac=str(tools/'jdk17/bin/javac') if (tools/'jdk17/bin/javac').exists() else shutil.which('javac')
    java=str(tools/'jdk17/bin/java') if (tools/'jdk17/bin/java').exists() else shutil.which('java')
    if not javac or not java:pytest.skip('JDK required for native queue integration')
    jar=tools/'test-json/json.jar'
    if not jar.exists():
        jar=tmp_path/'json.jar'
        with urllib.request.urlopen('https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar',timeout=20) as response:jar.write_bytes(response.read(256*1024))
    assert hashlib.sha256(jar.read_bytes()).hexdigest()=='3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed'
    fixtures={
        'android/content/Context.java':'package android.content;public class Context {public static final String CONNECTIVITY_SERVICE="network";public Context getApplicationContext(){return this;}public Object getSystemService(String id){return new android.net.ConnectivityManager();}}',
        'android/net/ConnectivityManager.java':'package android.net;public class ConnectivityManager {public static volatile boolean online=true;public Network getActiveNetwork(){return new Network();}public NetworkCapabilities getNetworkCapabilities(Network n){return online?new NetworkCapabilities():null;}}',
        'android/net/Network.java':'package android.net;public class Network {public java.net.URLConnection openConnection(java.net.URL u)throws java.io.IOException{return u.openConnection();}}',
        'android/net/NetworkCapabilities.java':'package android.net;public class NetworkCapabilities {public static final int NET_CAPABILITY_INTERNET=1,NET_CAPABILITY_VALIDATED=2;public boolean hasCapability(int n){return n==NET_CAPABILITY_INTERNET;}}',
        'android/os/SystemClock.java':'package android.os;public class SystemClock {public static long elapsedRealtime(){return System.nanoTime()/1000000;}}',
    }
    files=[]
    for name,source in fixtures.items():
        file=tmp_path/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(source);files.append(str(file))
    source=ROOT/'android/src/com/aidj/music'
    files.extend(str(source/name) for name in ['DownloadRunner.java','DownloadTransfer.java','DownloadPolicy.java','NetworkStatus.java'])
    files.append(str(ROOT/'tests/DownloadRunnerProbe.java'))
    subprocess.run([javac,'-J-Xmx128m','-J-XX:ActiveProcessorCount=2','--release','8','-classpath',str(jar),'-d',str(tmp_path),*files],check=True,capture_output=True,timeout=30)
    calls=[];counts={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            kind=self.path.rsplit('/',1)[-1][0];offset=int(self.headers.get('Range','bytes=0-')[6:-1]);calls.append((kind,offset))
            counts[kind]=counts.get(kind,0)+1
            if kind in ('c','f'):
                self.send_response(404 if kind=='c' else 401);self.send_header('Content-Length','0');self.end_headers();return
            if kind=='g' and counts[kind]==1:
                self.send_response(403);self.send_header('Content-Length','0');self.end_headers();return
            length={'a':300000,'b':200000,'d':1000000,'e':50000,'g':10000,'h':10000,'i':10000}[kind];body=kind.upper().encode()*length
            self.send_response(206 if offset else 200);self.send_header('Content-Length',str(length-offset));self.send_header('ETag','"'+hashlib.md5(body).hexdigest()+'"')
            if offset:self.send_header('Content-Range',f'bytes {offset}-{length-1}/{length}')
            self.end_headers()
            try:
                if kind=='a' and counts[kind]==1:
                    self.wfile.write(body[:100000]);self.wfile.flush();self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                for start in range(offset,length,16384):
                    self.wfile.write(body[start:start+16384]);self.wfile.flush()
                    if kind=='d' and counts[kind]==1:time.sleep(.02)
            except (ConnectionResetError,BrokenPipeError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    data=tmp_path/'audio';data.mkdir()
    try:
        result=subprocess.run([java,'-Xmx64m','-XX:ActiveProcessorCount=2','-classpath',str(tmp_path)+':'+str(jar),'com.aidj.music.DownloadRunnerProbe',f'http://127.0.0.1:{server.server_port}',str(data)],capture_output=True,text=True,timeout=35)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'RUNNER_PASSED' in result.stdout
        assert counts['a']==2 and ('a',100000) in calls
        assert counts['b']==1 and counts['d']==2 and any(k=='d' and o>0 for k,o in calls)
        assert counts['c']==1 and counts['f']==1 and counts['e']==1
        assert counts['g']==2 and counts['h']==1 and counts['i']==1
    finally:server.shutdown();server.server_close();thread.join()
