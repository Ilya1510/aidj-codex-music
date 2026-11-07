import json
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self,config):
        self.base='https://api.telegram.org/bot'+config['token']+'/'
        self.session=requests.Session()

    def call(self,method,data=None,timeout=40):
        try:
            response=self.session.post(self.base+method,json=data or {},timeout=(15,timeout))
            body=response.json()
        except Exception:
            raise TelegramError('Не удалось соединиться с Telegram.') from None
        if not body.get('ok'):
            if method in ('editMessageText','editMessageReplyMarkup') and 'message is not modified' in body.get('description',''):
                return None
            raise TelegramError('Telegram не принял '+method+' (код '+str(body.get('error_code',response.status_code))+').')
        return body.get('result')

    def message(self,chat_id,text,keyboard=None):
        data={'chat_id':chat_id,'text':text[:4096],'link_preview_options':{'is_disabled':True}}
        if keyboard:
            data['reply_markup']={'inline_keyboard':keyboard}
        return self.call('sendMessage',data)

    def edit(self,chat_id,message_id,text,keyboard=None):
        data={'chat_id':chat_id,'message_id':message_id,'text':text[:4096],
              'link_preview_options':{'is_disabled':True}}
        if keyboard is not None:
            data['reply_markup']={'inline_keyboard':keyboard}
        return self.call('editMessageText',data)

    def markup(self,chat_id,message_id,keyboard):
        return self.call('editMessageReplyMarkup',{'chat_id':chat_id,'message_id':message_id,
                         'reply_markup':{'inline_keyboard':keyboard}})

    def audio(self,chat_id,result,keyboard=None):
        track=result['track']
        custom=result.get('caption')
        caption=(custom if isinstance(custom,str) else track['artist']+' — '+track['title'])[:1000]
        keyboard={'inline_keyboard':keyboard or [[{'text':'Открыть на Диске','url':result['disk_url']},
                                                  {'text':'Источник','url':track['url']}]]}
        fields={'chat_id':str(chat_id),'caption':caption,'performer':track['artist'][:64],
                'title':track['title'][:64],'duration':str(round(result['duration'])),
                'reply_markup':json.dumps(keyboard,ensure_ascii=False)}
        if result.get('telegram_file_id'):
            return self.call('sendAudio',{**fields,'audio':result['telegram_file_id']},timeout=90)
        local=Path(result['local_path'])
        try:
            with local.open('rb') as f:
                fields['audio']=(local.name,f,'audio/mpeg' if local.suffix=='.mp3' else 'audio/mp4')
                stream=MultipartEncoder(fields=fields)
                r=self.session.post(self.base+'sendAudio',data=stream,headers={'Content-Type':stream.content_type},timeout=(15,180))
                body=r.json()
        except Exception:
            raise TelegramError('Аудио сохранено на Диск, но Telegram не принял файл.') from None
        if not body.get('ok'):
            raise TelegramError('Аудио сохранено на Диск, но Telegram не принял файл.')
        return body['result']
