"""Read-only library check through the actual app queue; no Telegram messages."""
import time
import uuid
import requests
from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest


def main(reuse_search=None):
    settings=Settings();db=connect(settings);token=create_session(db);client=requests.Session()
    client.cookies.set('aidj_session',token);client.headers['X-AIDJ-App']='1';base='http://127.0.0.1:18782/api/'
    before=[tuple(r) for r in db.execute('SELECT playlist_id,track_key,position FROM playlist_tracks ORDER BY playlist_id,position')]
    cache_count=db.execute('SELECT count(*) FROM cache').fetchone()[0]
    jobs=[]
    def submit(body):
        r=client.post(base+'discover',json={**body,'opId':str(uuid.uuid4())},timeout=15)
        assert r.status_code==200,'APP_SUBMIT_FAILED_'+str(r.status_code)
        job=r.json();jobs.append(job['id']);return job
    def completed(job):
        deadline=time.monotonic()+120;last=None
        while time.monotonic()<deadline:
            r=client.get(base+'discover/'+str(job['id']),timeout=15);assert r.status_code==200
            result=r.json()
            if result['status']!=last:print('LIVE_APP_JOB',job['id'],result['status'],flush=True);last=result['status']
            if result['status']=='completed':return result
            assert result['status'] not in ('failed','awaiting_choice'),'APP_JOB_NOT_COMPLETED'
            time.sleep(1)
        raise AssertionError('APP_QUEUE_TIMEOUT')
    try:
        found=completed({'id':reuse_search}) if reuse_search else completed(submit({'action':'search','query':'София Ротару Белая зима'}))
        index=next(c['index'] for c in found['candidates'] if c['inLibrary'] and c['title']=='Белая зима' and 'Ротару' in c['artist'])
        chosen=completed(submit({'action':'prepare','jobId':found['id'],'index':index}))
        assert chosen['trackId']
        r=client.get(base+'audio/'+chosen['trackId'],headers={'Range':'bytes=0-1023'},timeout=30)
        assert r.status_code==206 and len(r.content)==1024
        after=[tuple(r) for r in db.execute('SELECT playlist_id,track_key,position FROM playlist_tracks ORDER BY playlist_id,position')]
        assert after==before and db.execute('SELECT count(*) FROM cache').fetchone()[0]==cache_count
        print('LIVE_DISCOVER_PASSED real_source_search ready_existing_asset audio_range no_redownload no_playlist_changes',flush=True)
    finally:
        client.close();db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(token),));db.commit()


if __name__=='__main__':
    try:main()
    except Exception as error:print('LIVE_DISCOVER_FAILED',type(error).__name__,flush=True);raise SystemExit(1)
