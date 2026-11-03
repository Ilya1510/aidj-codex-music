from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading

from aidj.telegram import Telegram


def test_streamed_audio_upload_includes_file_caption_and_disk_link(tmp_path):
    captured={}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size=int(self.headers['Content-Length'])
            captured['body']=self.rfile.read(size)
            captured['type']=self.headers['Content-Type']
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps({'ok':True,'result':{'audio':{'file_id':'stored-file'}}}).encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    audio=tmp_path/'Песня.mp3'
    audio.write_bytes(b'AUDIO_FRAME'*10000)
    bot=Telegram({'token':'FAKE_PRIVATE_TOKEN'})
    bot.base=f'http://127.0.0.1:{server.server_port}/'
    result={'track':{'title':'Песня','artist':'Исполнитель','url':'https://music.yandex.ru/track/123'},
            'trim':{'removed_start':1.0,'removed_end':2.0},'duration':120,
            'disk_url':'https://disk.yandex.ru/client/disk/Музыка/test',
            'local_path':str(audio),'telegram_file_id':None}
    try:
        response=bot.audio(1,result)
    finally:
        server.shutdown();server.server_close();thread.join()
    assert response['audio']['file_id']=='stored-file'
    assert captured['type'].startswith('multipart/form-data')
    assert b'AUDIO_FRAME' in captured['body'] and 'Песня'.encode() in captured['body']
    assert b'disk.yandex.ru' in captured['body']
    assert b'FAKE_PRIVATE_TOKEN' not in captured['body']
    assert 'тишин'.encode() not in captured['body']
    assert 'обрез'.encode() not in captured['body']
