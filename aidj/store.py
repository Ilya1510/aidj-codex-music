import hashlib
import json
import sqlite3
import time


def install_dj_asset_triggers(db):
    """Every committed eligible app asset joins the durable full-library run.

    The application transaction owns this enqueue too. Repeated aliases,
    metadata refreshes and failed records cannot reset an existing job/lease.
    """
    from .dj import VERSION,MAX_FULL_DURATION
    run=hashlib.sha256(('full:'+str(VERSION)+':all').encode()).hexdigest()[:24]
    eligible=f"NEW.duration>0 AND NEW.duration<={MAX_FULL_DURATION} AND NEW.bytes>0 AND NEW.bytes<=41943040"
    version=hashlib.sha256(('2:'+run+eligible).encode()).hexdigest()
    installed=db.execute("SELECT value FROM meta WHERE key='dj_asset_trigger_version'").fetchone()
    if installed and installed[0]==version:return
    cached="EXISTS(SELECT 1 FROM dj_evidence e WHERE e.md5=NEW.md5 AND json_extract(e.payload,'$.fullComplete')=1 AND COALESCE(json_extract(e.payload,'$.boundaryPass'),0)>=1 AND json_extract(e.payload,'$.version')>="+str(VERSION)+")"
    body=f"""
        INSERT INTO dj_batch_runs(id,scope,created)
        SELECT '{run}','all',CAST(strftime('%s','now') AS REAL) WHERE NOT EXISTS(SELECT 1 FROM dj_batch_runs WHERE id='{run}');
        INSERT INTO dj_batch_jobs(run_id,md5,asset_id,state,lease,attempts,error,updated)
        SELECT '{run}',NEW.md5,NEW.id,CASE WHEN {cached} THEN 'done' ELSE 'pending' END,0,0,
                CASE WHEN {cached} THEN NULL ELSE 'NEW_ASSET' END,CAST(strftime('%s','now') AS REAL)
        WHERE NOT EXISTS(SELECT 1 FROM dj_batch_jobs WHERE run_id='{run}' AND md5=NEW.md5);
    """
    db.executescript(f"""
        BEGIN IMMEDIATE;
        DROP TRIGGER IF EXISTS dj_enrol_asset_insert;
        DROP TRIGGER IF EXISTS dj_enrol_asset_content;
        CREATE TRIGGER IF NOT EXISTS dj_enrol_asset_insert AFTER INSERT ON web_assets
        WHEN {eligible} BEGIN {body} END;
        CREATE TRIGGER IF NOT EXISTS dj_enrol_asset_content AFTER UPDATE OF md5,duration,bytes ON web_assets
        WHEN {eligible} AND (OLD.md5!=NEW.md5 OR OLD.duration<=0 OR OLD.duration>{MAX_FULL_DURATION} OR OLD.bytes<=0 OR OLD.bytes>41943040)
        BEGIN {body} END;
        INSERT OR REPLACE INTO meta VALUES ('dj_asset_trigger_version','{version}');
        COMMIT;
    """)


def connect(settings,*,dj_enrol=True):
    settings.prepare()
    db=sqlite3.connect(settings.state/'bot.sqlite',timeout=30)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY,update_id TEXT UNIQUE,chat_id INTEGER,user_id INTEGER,
        message_id INTEGER,query TEXT,payload TEXT,status TEXT DEFAULT 'queued',
        stage TEXT,error TEXT,result TEXT,created REAL,updated REAL);
    CREATE TABLE IF NOT EXISTS choices(
        id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,query TEXT,
        tracks TEXT,used INTEGER DEFAULT 0,created REAL);
    CREATE TABLE IF NOT EXISTS cache(
        key TEXT PRIMARY KEY,result TEXT,telegram_file_id TEXT,created REAL);
    CREATE TABLE IF NOT EXISTS playlists(
        id INTEGER PRIMARY KEY,name TEXT NOT NULL,name_key TEXT UNIQUE NOT NULL,
        path TEXT UNIQUE NOT NULL,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS playlist_tracks(
        playlist_id INTEGER NOT NULL,track_key TEXT NOT NULL,position INTEGER NOT NULL,
        remote_path TEXT NOT NULL,md5 TEXT NOT NULL,bytes INTEGER NOT NULL,
        duration REAL NOT NULL,label TEXT NOT NULL,state TEXT NOT NULL,
        PRIMARY KEY(playlist_id,track_key),UNIQUE(playlist_id,position));
    CREATE TABLE IF NOT EXISTS playlist_aliases(
        playlist_id INTEGER NOT NULL,track_key TEXT NOT NULL,canonical_key TEXT NOT NULL,
        PRIMARY KEY(playlist_id,track_key));
    CREATE TABLE IF NOT EXISTS playlist_selections(
        id TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
        message_id INTEGER,result TEXT NOT NULL,selected TEXT NOT NULL DEFAULT '[]',
        state TEXT NOT NULL DEFAULT 'open',created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS playlist_clicks(callback_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS web_sessions(digest TEXT PRIMARY KEY,expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS web_login_codes(digest TEXT PRIMARY KEY,expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS web_assets(
        id TEXT PRIMARY KEY,track_key TEXT UNIQUE NOT NULL,title TEXT NOT NULL,artist TEXT NOT NULL,
        duration REAL NOT NULL,md5 TEXT NOT NULL,bytes INTEGER NOT NULL,remote_path TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS web_operations(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS native_transfer(id INTEGER PRIMARY KEY CHECK(id=1),descriptor TEXT NOT NULL,expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS app_deliveries(id TEXT PRIMARY KEY,md5 TEXT UNIQUE NOT NULL,asset_id TEXT NOT NULL,revision INTEGER NOT NULL,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS app_delivery_acks(delivery_id TEXT NOT NULL,device_id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,PRIMARY KEY(delivery_id,device_id));
    CREATE TABLE IF NOT EXISTS app_keyboard_updates(sid TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS device_diagnostics(device_id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS library_added(track_key TEXT PRIMARY KEY,added REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS audio_clips(track_key TEXT PRIMARY KEY,result TEXT NOT NULL,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dj_batch_runs(id TEXT PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dj_batch_jobs(run_id TEXT NOT NULL,md5 TEXT NOT NULL,asset_id TEXT NOT NULL,state TEXT NOT NULL,
        lease REAL NOT NULL DEFAULT 0,token TEXT,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,updated REAL NOT NULL,
        PRIMARY KEY(run_id,md5));
    CREATE INDEX IF NOT EXISTS dj_batch_lease ON dj_batch_jobs(md5,state,lease);
    CREATE TABLE IF NOT EXISTS dj_runtime(id INTEGER PRIMARY KEY CHECK(id=1),heartbeat REAL NOT NULL,phase TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS dj_priority(md5 TEXT PRIMARY KEY,rank INTEGER NOT NULL,requested REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dj_evidence(md5 TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dj_analysis(md5 TEXT PRIMARY KEY,asset_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS playback_ranges(track_id TEXT PRIMARY KEY,start REAL NOT NULL,end REAL NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_users(user_id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,
        playlist_id INTEGER NOT NULL,mode TEXT NOT NULL DEFAULT 'menu',created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_profiles(user_id INTEGER PRIMARY KEY,username TEXT,
        display_name TEXT NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_items(id TEXT PRIMARY KEY,track_key TEXT UNIQUE NOT NULL,
        result TEXT NOT NULL,telegram_file_id TEXT,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_cards(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,
        playlist_id INTEGER NOT NULL,item_id TEXT NOT NULL,message_id INTEGER,
        state TEXT NOT NULL DEFAULT 'queued',created REAL NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS review_one_active ON review_cards(user_id) WHERE state IN ('queued','open');
    CREATE TABLE IF NOT EXISTS review_votes(playlist_id INTEGER NOT NULL,item_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,value INTEGER NOT NULL CHECK(value IN (-1,0,1)),updated REAL NOT NULL,
        PRIMARY KEY(playlist_id,item_id,user_id));
    CREATE TABLE IF NOT EXISTS review_proposals(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,
        playlist_id INTEGER NOT NULL,item_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'preview',created REAL NOT NULL,
        UNIQUE(user_id,playlist_id,item_id));
    CREATE TABLE IF NOT EXISTS review_choices(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,
        playlist_id INTEGER NOT NULL,query TEXT NOT NULL,tracks TEXT NOT NULL,youtube INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS review_votes_item ON review_votes(playlist_id,item_id);
    CREATE INDEX IF NOT EXISTS review_proposals_pool ON review_proposals(playlist_id,status,item_id);
    CREATE TABLE IF NOT EXISTS review_saves(item_id TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,error TEXT,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_notices(playlist_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
        md5 TEXT NOT NULL,chat_id INTEGER NOT NULL,item_id TEXT NOT NULL,label TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,sent REAL,
        PRIMARY KEY(playlist_id,user_id,md5));
    CREATE TABLE IF NOT EXISTS review_removal_notices(operation_id TEXT NOT NULL,playlist_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,md5 TEXT NOT NULL,chat_id INTEGER NOT NULL,label TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,sent REAL,
        PRIMARY KEY(operation_id,user_id));
    CREATE TABLE IF NOT EXISTS review_web_ops(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
    ''')
    if dj_enrol:install_dj_asset_triggers(db)
    return db


def meta_get(db,key,default=None):
    r=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return r[0] if r else default


def meta_set(db,key,value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
    db.commit()


def job_update(db,job_id,**fields):
    fields['updated']=time.time()
    db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', [*fields.values(),job_id])
    db.commit()


def enqueue(db,update_id,chat_id,user_id,message_id,query,payload=None):
    now=time.time()
    try:
        cursor=db.execute('INSERT INTO jobs(update_id,chat_id,user_id,message_id,query,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                          (str(update_id),chat_id,user_id,message_id,query,json.dumps(payload or {},ensure_ascii=False),now,now))
        db.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
        return None


def claim_job(db):
    db.execute('BEGIN IMMEDIATE')
    row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
    if row:
        db.execute("UPDATE jobs SET status='processing',updated=? WHERE id=?",(time.time(),row['id']))
    db.commit()
    return row
