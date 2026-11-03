import json
import time

from .playlists import Playlists, PlaylistError, playlist_name
from .store import enqueue, meta_get, meta_set


class PlaylistUI:
    def __init__(self, db, bot, settings):
        self.db, self.bot, self.settings = db, bot, settings
        self.catalog = Playlists(settings, db)

    def submit(self, update_id, chat_id, user_id, text, payload):
        old = self.db.execute('SELECT id FROM jobs WHERE update_id=?',(str(update_id),)).fetchone()
        if old:
            return old[0]
        if self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0] >= 10:
            raise PlaylistError('В очереди уже десять запросов. Дождись обработки.')
        sent = self.bot.message(chat_id, 'Добавляю…' if payload['action']=='playlist_add' else 'Создаю плейлист…')
        return enqueue(self.db,update_id,chat_id,user_id,sent['message_id'],text,payload)

    def prompt(self, chat_id, user_id, sid=None):
        self.bot.message(chat_id, 'Как назвать плейлист? Напиши название. /cancel — отменить.')
        meta_set(self.db,'playlist_name_prompt',json.dumps({'chat_id':chat_id,'user_id':user_id,'sid':sid}))

    def create(self, update_id, chat_id, user_id, name, sid=None):
        try:
            name = playlist_name(name)
            self.submit(update_id,chat_id,user_id,'Новый плейлист: ' + name,
                        {'action':'playlist_create','name':name,'selection_id':sid})
            meta_set(self.db,'playlist_name_prompt','')
        except PlaylistError as exc:
            self.bot.message(chat_id,str(exc))

    def pending_name(self, update_id, chat_id, user_id, text):
        pending = meta_get(self.db,'playlist_name_prompt','')
        if not pending:
            return False
        value = json.loads(pending)
        if value['chat_id']!=chat_id or value['user_id']!=user_id:
            return False
        self.create(update_id,chat_id,user_id,text,value.get('sid'))
        return True

    def callback(self, update):
        cb = update['callback_query']
        parts = cb.get('data','').split(':')
        if len(parts)<2 or parts[0] not in ('pl','pa','pn','pd'):
            return False
        chat_id, user_id = cb['message']['chat']['id'], cb['from']['id']
        row = self.db.execute('SELECT * FROM playlist_selections WHERE id=?',(parts[1],)).fetchone()
        def answer(text):
            self.bot.call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':text})
        if not row or row['chat_id']!=chat_id or row['user_id']!=user_id:
            answer('Этот выбор недоступен.')
            return True
        if row['state']!='open':
            answer('Добавляю…')
            return True
        sid = row['id']
        if parts[0]=='pn':
            answer('Введи название в чат')
            self.prompt(chat_id,user_id,sid)
            return True
        if parts[0] in ('pl','pd'):
            payload={'action':'playlist_device','selection_id':sid}
            if parts[0]=='pl':
                try:playlist_id=int(parts[2])
                except (ValueError,IndexError):answer('Плейлист не найден.');return True
                playlist=self.db.execute('SELECT * FROM playlists WHERE id=?',(playlist_id,)).fetchone()
                if not playlist:answer('Плейлист не найден.');return True
                if self.catalog.contains(playlist_id,json.loads(row['result'])):
                    self.catalog.refresh(self.bot,sid);answer('Уже в этом плейлисте');return True
                payload={'action':'playlist_add_one','selection_id':sid,'playlist_id':playlist_id}
            self.db.execute('BEGIN IMMEDIATE')
            try:
                repeated=self.db.execute('SELECT 1 FROM playlist_clicks WHERE callback_id=?',(cb['id'],)).fetchone()
                same=self.db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','processing') AND json_extract(payload,'$.selection_id')=? AND json_extract(payload,'$.action')=? AND COALESCE(json_extract(payload,'$.playlist_id'),0)=?",(sid,payload['action'],payload.get('playlist_id',0))).fetchone()
                if not repeated and not same:
                    if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]>=10:raise PlaylistError('В очереди уже десять запросов. Дождись обработки.')
                    now=time.time()
                    self.db.execute('INSERT INTO jobs(update_id,chat_id,user_id,message_id,query,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                                    (str(update['update_id']),chat_id,user_id,0,'Скачать в приложение' if parts[0]=='pd' else 'Добавить в плейлист',json.dumps(payload),now,now))
                    self.db.execute('INSERT INTO playlist_clicks VALUES (?)',(cb['id'],))
                self.db.commit()
            except BaseException:self.db.rollback();raise
            self.catalog.refresh(self.bot,sid);answer('Принял');return True
        # Old messages may still contain the former confirmation callback.
        if not json.loads(row['selected']):
            answer('Сначала отметь плейлисты')
            return True
        self.db.execute("UPDATE playlist_selections SET state='queued' WHERE id=? AND state='open'",(sid,))
        self.db.commit()
        try:
            self.submit(update['update_id'],chat_id,user_id,'Добавить в плейлисты',
                        {'action':'playlist_add','selection_id':sid})
        except Exception:
            self.db.execute("UPDATE playlist_selections SET state='open' WHERE id=?",(sid,))
            self.db.commit()
            raise
        answer('Добавляю')
        return True
