"""Exercise real Disk edits only inside a disposable, uniquely named playlist."""
import json
from pathlib import PurePosixPath
import time
import uuid

import requests

from aidj.config import Settings
from aidj.disk import Disk
from aidj.store import connect
from aidj.web_auth import create_session,digest
from aidj.web_catalog import catalog


def main():
    settings=Settings();db=connect(settings);disk=Disk(settings)
    origin=json.loads((settings.state/'miniapp-url.json').read_text())['url']
    token=create_session(db);client=requests.Session()
    client.headers.update({'Cookie':'aidj_session='+token,'X-AIDJ-App':'1'})
    marker='qa_'+uuid.uuid4().hex[:12];name='AI DJ QA '+marker[3:]
    playlist_id=None;stage='catalog';operation_ids=[];remove_target=None
    originals=[]
    def get():
        r=client.get(origin+'/api/catalog',timeout=30);r.raise_for_status();return r.json()
    def edit(action,**extra):
        values=get();p=next((p for p in values['playlists'] if p['id']==extra.get('playlistId')),None)
        op=marker+'_'+str(len(operation_ids));operation_ids.append(op)
        body={'action':action,'opId':op,'revision':p['revision'] if p else None,**extra}
        r=client.post(origin+'/api/edit',json=body,timeout=60)
        if r.status_code!=200:raise RuntimeError('EDIT_HTTP_'+str(r.status_code))
        return r.json(),op
    try:
        initial=get();ids=set(initial['playlists'][0]['trackIds'])
        selected=sorted((t for t in initial['tracks'] if t['id'] in ids),key=lambda t:t['bytes'])[:2]
        originals=[dict(db.execute('SELECT * FROM web_assets WHERE id=?',(t['id'],)).fetchone()) for t in selected]
        stage='create';values,_=edit('create',name=name)
        playlist_id=next(p['id'] for p in values['playlists'] if p['name']==name)
        for track in selected:
            stage='add';values,_=edit('add',playlistId=playlist_id,trackId=track['id'])
        stage='reorder';values,_=edit('reorder',playlistId=playlist_id,trackIds=[t['id'] for t in reversed(selected)])
        assert next(p for p in values['playlists'] if p['id']==playlist_id)['trackIds']==[t['id'] for t in reversed(selected)]
        stage='rename';values,_=edit('rename',playlistId=playlist_id,name=name+' R')
        stage='remove';values,op=edit('remove',playlistId=playlist_id,trackId=selected[0]['id'])
        plan=json.loads(db.execute('SELECT payload FROM web_operations WHERE id=?',(op,)).fetchone()[0]);remove_target=plan['target']
        assert next(p for p in values['playlists'] if p['id']==playlist_id)['trackIds']==[selected[1]['id']]
        for source in originals:assert disk.verify(source['remote_path'],source['md5'],source['bytes'])
        print('LIVE_DISK_EDITS_PASSED create add reorder rename remove originals_preserved',flush=True)
    except Exception as exc:
        print('LIVE_DISK_EDITS_FAILED',stage,type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else '',flush=True)
        raise
    finally:
        if playlist_id:
            row=db.execute('SELECT * FROM playlists WHERE id=?',(playlist_id,)).fetchone()
            if row and row['name'].startswith(name):
                items=list(disk.items(row['path']))
                expected_md5={x['md5'] for x in originals}
                assert all(i['type']=='file' and (i['name'].endswith('.m3u8') or i['md5'] in expected_md5) for i in items)
                r=disk.request('DELETE','/resources',params={'path':row['path'],'permanently':'false'})
                if r.status_code==202:disk.wait_operation(r)
                else:assert r.status_code==204
                db.execute('DELETE FROM playlist_tracks WHERE playlist_id=?',(playlist_id,))
                db.execute('DELETE FROM playlist_aliases WHERE playlist_id=?',(playlist_id,))
                db.execute('DELETE FROM playlists WHERE id=?',(playlist_id,));db.commit()
        if remove_target and marker in PurePosixPath(remove_target).name:
            r=disk.request('DELETE','/resources',params={'path':remove_target,'permanently':'false'})
            if r.status_code==202:disk.wait_operation(r)
            else:assert r.status_code in (204,404)
            parent=str(PurePosixPath(remove_target).parent)
            if disk.metadata(parent) and not list(disk.items(parent)):disk.trash_empty(parent)
        for op in operation_ids:db.execute('DELETE FROM web_operations WHERE id=?',(op,))
        db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(token),));db.commit()
        catalog(db);client.close()
        print('LIVE_TEST_PLAYLIST_CLEANED',flush=True)


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit(1)
