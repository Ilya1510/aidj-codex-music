"""Durable downloads requested in Telegram, independently acknowledged per phone."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets
import time

from .web_catalog import asset_id, catalog


def library_revision(db):
    return hashlib.sha256(json.dumps(catalog(db),ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]


def publish(db,result):
    track=result['track'];key=track['source']+':'+str(track['id']);aid=asset_id(key)
    db.execute('INSERT OR IGNORE INTO web_assets VALUES (?,?,?,?,?,?,?,?)',
               (aid,key,track['title']+(' ('+track['version']+')' if track.get('version') else ''),track['artist'],result['duration'],result['md5'],result['bytes'],result['remote_path']))
    db.execute('INSERT OR IGNORE INTO library_added VALUES (?,?)',(key,time.time()))
    db.execute('''INSERT INTO app_deliveries VALUES (?,?,?,1,?) ON CONFLICT(md5) DO UPDATE SET
        asset_id=excluded.asset_id,revision=app_deliveries.revision+1,created=excluded.created''',
               (secrets.token_hex(12),result['md5'],aid,time.time()))
    db.commit()


def state(db,md5):
    row=db.execute('SELECT * FROM app_deliveries WHERE md5=?',(md5,)).fetchone()
    if not row:return None
    ready=db.execute("SELECT 1 FROM app_delivery_acks WHERE delivery_id=? AND revision=? AND status='ready'",(row['id'],row['revision'])).fetchone()
    statuses=[r[0] for r in db.execute('SELECT status FROM app_delivery_acks WHERE delivery_id=? AND revision=?',(row['id'],row['revision']))]
    if statuses and all(value=='cancelled' for value in statuses):return None
    return 'ready' if ready else 'queued'


def device(body):
    value=body.get('deviceId','')
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',value):raise ValueError('invalid device')
    return value


def pending(db,body):
    did=device(body);revision=library_revision(db)
    rows=db.execute('''SELECT d.*,a.title,a.artist,a.duration,a.bytes,a.remote_path FROM app_deliveries d
        JOIN web_assets a ON a.id=d.asset_id LEFT JOIN app_delivery_acks k ON k.delivery_id=d.id AND k.device_id=?
        WHERE k.revision IS NULL OR k.revision<d.revision ORDER BY d.created LIMIT 50''',(did,)).fetchall()
    return {'revision':revision,'items':[{'id':r['id'],'revision':r['revision'],'track':{'id':r['asset_id'],
        'title':r['title'],'artist':r['artist'],'duration':r['duration'],'bytes':r['bytes'],'md5':r['md5'],
        'format':PurePosixPath(r['remote_path']).suffix[1:]}} for r in rows]}


def acknowledge(db,body):
    did=device(body);items=body.get('items')
    if not isinstance(items,list) or len(items)>100:raise ValueError('invalid receipt')
    validated=[]
    for item in items:
        if not isinstance(item,dict) or item.get('status') not in ('queued','ready','cancelled') or type(item.get('revision')) is not int or not isinstance(item.get('id'),str):raise ValueError('invalid receipt')
        validated.append(item)
    db.execute('BEGIN IMMEDIATE')
    try:
        for item in validated:
            row=db.execute('SELECT * FROM app_deliveries WHERE id=?',(item['id'],)).fetchone()
            if not row or row['revision']!=item['revision'] or row['md5']!=item.get('md5'):continue
            old=db.execute('SELECT * FROM app_delivery_acks WHERE delivery_id=? AND device_id=?',(row['id'],did)).fetchone()
            if old and old['revision']==row['revision'] and old['status']=='ready':continue
            db.execute('''INSERT INTO app_delivery_acks VALUES (?,?,?,?) ON CONFLICT(delivery_id,device_id)
                DO UPDATE SET revision=excluded.revision,status=excluded.status''',(row['id'],did,row['revision'],item['status']))
            if item['status'] in ('ready','cancelled'):
                db.execute("INSERT OR IGNORE INTO app_keyboard_updates SELECT id FROM playlist_selections WHERE message_id>0 AND json_extract(result,'$.md5')=?",(row['md5'],))
        db.commit()
    except BaseException:db.rollback();raise
    return {'ok':True}
