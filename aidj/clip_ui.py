import json
from .clips import span
from .audio import AudioError
from .store import meta_get,meta_set,enqueue


class ClipUI:
    def __init__(self,db,bot):self.db,self.bot=db,bot
    def callback(self,update):
        cb=update['callback_query'];parts=cb.get('data','').split(':')
        if len(parts)!=2 or parts[0]!='pc':return False
        row=self.db.execute('SELECT * FROM playlist_selections WHERE id=?',(parts[1],)).fetchone()
        chat=cb['message']['chat']['id'];user=cb['from']['id']
        if not row or row['chat_id']!=chat or row['user_id']!=user:return False
        meta_set(self.db,'clip_prompt',json.dumps({'sid':row['id'],'chat':chat,'user':user}))
        self.bot.call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':'Укажи интервал'})
        self.bot.message(chat,'Напиши, какой фрагмент сохранить: например, 0:00–1:30.\nОригинал останется целым. /cancel — отменить.')
        return True
    def pending(self,update_id,chat,user,text):
        pending=meta_get(self.db,'clip_prompt','')
        if not pending:return False
        pending=json.loads(pending)
        if pending['chat']!=chat or pending['user']!=user:return False
        row=self.db.execute('SELECT result FROM playlist_selections WHERE id=?',(pending['sid'],)).fetchone()
        if not row:meta_set(self.db,'clip_prompt','');return False
        try:start,end=span(text,json.loads(row[0])['duration'])
        except AudioError as error:self.bot.message(chat,str(error));return True
        if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]>=10:
            self.bot.message(chat,'В очереди уже десять запросов. Дождись обработки.');return True
        sent=self.bot.message(chat,'Готовлю фрагмент…')
        enqueue(self.db,update_id,chat,user,sent['message_id'],'Фрагмент '+text,{'action':'clip','selection_id':pending['sid'],'start':start,'end':end})
        meta_set(self.db,'clip_prompt','');return True
