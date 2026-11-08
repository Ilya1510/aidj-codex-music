"""A small catalog of Disk folders, with resumable additions and M3U indexes."""
import json
import hashlib
from pathlib import Path, PurePosixPath
import re
import secrets
import time
import unicodedata

from .disk import Disk, DiskError, web_url
from .pipeline import safe_filename


class PlaylistError(RuntimeError):
    pass


def playlist_name(value):
    name = unicodedata.normalize('NFC', value).strip()
    if (not name or len(name) > 64 or len(name.encode()) > 150 or
        name.startswith(('.', '_')) or name.endswith('.') or
        re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', name) or
        name.casefold() == 'aidj_codex_music'):
        raise PlaylistError('Название: до 64 символов, без слешей и служебных символов.')
    return name


def track_key(result):
    track = result['track']
    return track['source'] + ':' + str(track['id'])


class Playlists:
    def __init__(self, settings, db, disk=None):
        self.settings, self.db = settings, db
        self.disk = disk

    def storage(self):
        if self.disk is None:
            self.disk = Disk(self.settings)
        return self.disk

    def list(self):
        return self.db.execute('''SELECT p.*,COUNT(t.track_key) AS tracks FROM playlists p
            LEFT JOIN playlist_tracks t ON t.playlist_id=p.id AND t.state='complete'
            GROUP BY p.id ORDER BY p.id''').fetchall()

    def create(self, value):
        name = playlist_name(value)
        key = name.casefold()
        from .playlist_combined import name_exists
        if name_exists(self.db,key):raise PlaylistError('Такое название уже есть.')
        existing = self.db.execute('SELECT * FROM playlists WHERE name_key=?',(key,)).fetchone()
        if existing:
            return existing
        if self.db.execute('SELECT COUNT(*) FROM playlists').fetchone()[0] >= 30:
            raise PlaylistError('Уже создано 30 плейлистов.')
        path = '/Музыка/' + name
        disk = self.storage()
        if disk.metadata(path):
            raise PlaylistError('На Диске уже есть такая папка. Выбери другое название.')
        disk.mkdir(path)
        self.db.execute('INSERT INTO playlists(name,name_key,path,created) VALUES (?,?,?,?)',
                        (name,key,path,time.time()))
        self.db.commit()
        return self.db.execute('SELECT * FROM playlists WHERE name_key=?',(key,)).fetchone()

    def key_for(self, playlist_id, result):
        key = track_key(result)
        for candidate in dict.fromkeys([result.get('origin_key',key),key]):
            alias = self.db.execute('SELECT canonical_key FROM playlist_aliases WHERE playlist_id=? AND track_key=?',
                                    (playlist_id,candidate)).fetchone()
            if alias:
                return alias[0]
            if self.db.execute('SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_key=?',(playlist_id,candidate)).fetchone():
                return candidate
        # The same verified file can arrive under another provider ID or URL.
        same=self.db.execute("""SELECT track_key FROM playlist_tracks WHERE playlist_id=? AND md5=? AND bytes=?
            ORDER BY CASE WHEN state='complete' THEN 0 ELSE 1 END,position LIMIT 1""",
            (playlist_id,result['md5'],result['bytes'])).fetchone()
        if same:return same[0]
        return key

    def contains(self, playlist_id, result):
        row = self.db.execute('SELECT state FROM playlist_tracks WHERE playlist_id=? AND track_key=?',
                             (playlist_id,self.key_for(playlist_id,result))).fetchone()
        return bool(row and row['state'] == 'complete')

    def add(self, playlist_id, result, work):
        playlist = self.db.execute('SELECT * FROM playlists WHERE id=?',(playlist_id,)).fetchone()
        if not playlist:
            raise PlaylistError('Плейлист больше не существует.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            key = self.key_for(playlist_id, result)
            row = self.db.execute('SELECT * FROM playlist_tracks WHERE playlist_id=? AND track_key=?',
                                  (playlist_id,key)).fetchone()
            if not row:
                position = self.db.execute('SELECT COALESCE(MAX(position),0)+1 FROM playlist_tracks WHERE playlist_id=?',
                                           (playlist_id,)).fetchone()[0]
                label = result['track']['artist'] + ' — ' + result['track']['title']
                if result['track'].get('version'):
                    label += ' (' + result['track']['version'] + ')'
                suffix = PurePosixPath(result['remote_path']).suffix
                identity=' ['+hashlib.sha256(key.encode()).hexdigest()[:10]+']' if position>1 else ''
                remote = playlist['path'] + f'/{position:03d} - ' + safe_filename(label) + identity + suffix
                # Reserve temporary negative positions to satisfy the unique index.
                self.db.execute('UPDATE playlist_tracks SET position=-position-10000 WHERE playlist_id=?',(playlist_id,))
                self.db.execute('UPDATE playlist_tracks SET position=-position-9999 WHERE playlist_id=?',(playlist_id,))
                self.db.execute('''INSERT INTO playlist_tracks
                    (playlist_id,track_key,position,remote_path,md5,bytes,duration,label,state)
                    VALUES (?,?,?,?,?,?,?,?,?)''',(playlist_id,key,1,remote,result['md5'],
                    result['bytes'],result['duration'],label,'pending'))
                self.db.execute('INSERT OR IGNORE INTO library_added VALUES (?,?)',(key,time.time()))
                row = self.db.execute('SELECT * FROM playlist_tracks WHERE playlist_id=? AND track_key=?',
                                      (playlist_id,key)).fetchone()
            for alias in dict.fromkeys([track_key(result),result.get('origin_key')]):
                if alias and alias!=key:
                    self.db.execute('INSERT OR IGNORE INTO playlist_aliases VALUES (?,?,?)',(playlist_id,alias,key))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        disk = self.storage()
        target = disk.metadata(row['remote_path'])
        if target:
            if target.get('md5') != row['md5'] or target.get('size') != row['bytes']:
                raise PlaylistError('Файл в плейлисте изменён вручную. Автоматическую замену отменил.')
        else:
            if not disk.verify(result['remote_path'],row['md5'],row['bytes']):
                raise PlaylistError('Исходный файл на Диске изменился. Запроси песню заново.')
            disk.copy(result['remote_path'], row['remote_path'])
            if not disk.verify(row['remote_path'], row['md5'], row['bytes']):
                raise DiskError('Диск не подтвердил добавление песни.')
        self.db.execute("UPDATE playlist_tracks SET state='complete' WHERE playlist_id=? AND track_key=?",(playlist_id,key))
        from .web_catalog import register_asset
        register_asset(self.db,result,key=key,remote_path=row['remote_path'])
        self.db.commit()
        # Also repair an index after an interrupted earlier attempt.
        self.write_index(playlist_id, work)
        from .review_delivery import accepted
        accepted(self.db,playlist_id,result)
        return playlist

    def write_index(self, playlist_id, work):
        playlist = self.db.execute('SELECT * FROM playlists WHERE id=?',(playlist_id,)).fetchone()
        local = Path(work) / (playlist['name'] + '.m3u8')
        with local.open('w',encoding='utf-8',newline='\n') as f:
            f.write('#EXTM3U\n')
            for row in self.db.execute("SELECT * FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(playlist_id,)):
                label = re.sub(r'[\r\n]', ' ', row['label'])
                f.write(f'#EXTINF:{round(row["duration"])},{label}\n{PurePosixPath(row["remote_path"]).name}\n')
        self.storage().upload(local, playlist['path'] + '/' + local.name, overwrite=True)

    def selection(self, chat_id, user_id, result):
        value = dict(result)
        value.pop('local_path', None)
        sid = secrets.token_hex(8)
        self.db.execute('INSERT INTO playlist_selections(id,chat_id,user_id,result,created) VALUES (?,?,?,?,?)',
                        (sid,chat_id,user_id,json.dumps(value,ensure_ascii=False),time.time()))
        self.db.commit()
        return sid

    def keyboard(self, sid):
        row = self.db.execute('SELECT * FROM playlist_selections WHERE id=?',(sid,)).fetchone()
        result = json.loads(row['result'])
        keyboard = [[{'text':'На Диске','url':result['disk_url']},
                     {'text':'Источник','url':result['track']['url']}]]
        for playlist in self.list():
            present = self.contains(playlist['id'], result)
            pending=self.db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','processing') AND json_extract(payload,'$.selection_id')=? AND json_extract(payload,'$.playlist_id')=? AND json_extract(payload,'$.action')='playlist_add_one'",(sid,playlist['id'])).fetchone()
            last=self.db.execute("SELECT status FROM jobs WHERE json_extract(payload,'$.selection_id')=? AND json_extract(payload,'$.playlist_id')=? AND json_extract(payload,'$.action')='playlist_add_one' ORDER BY id DESC LIMIT 1",(sid,playlist['id'])).fetchone()
            prefix = '✓ ' if present else '☑ ' if pending else '⚠ ' if last and last[0]=='failed' else '☐ '
            keyboard.append([{'text':prefix + playlist['name'], 'callback_data':f'pl:{sid}:{playlist["id"]}'}])
        from .device_inbox import state
        delivery=state(self.db,result['md5'])
        pending=self.db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','processing') AND json_extract(payload,'$.selection_id')=? AND json_extract(payload,'$.action')='playlist_device'",(sid,)).fetchone()
        label='✓ Скачано в приложении' if delivery=='ready' else '☑ Загрузка в приложение' if pending or delivery else '☐ Скачать в приложение'
        keyboard.append([{'text':label,'callback_data':'pd:'+sid}])
        keyboard.append([{'text':'✂ Обрезать','callback_data':'pc:'+sid}])
        keyboard.append([{'text':'＋ Создать плейлист','callback_data':'pn:' + sid}])
        return keyboard

    def refresh(self, bot, sid):
        row = self.db.execute('SELECT * FROM playlist_selections WHERE id=?',(sid,)).fetchone()
        if row and row['message_id']:
            bot.markup(row['chat_id'],row['message_id'],self.keyboard(sid))
