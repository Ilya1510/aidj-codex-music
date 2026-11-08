import json
import time
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder


class TelegramError(RuntimeError):
    """Safe diagnostics: API numbers only, never its body, URL or description."""
    def __init__(self,message,*,code=None,retry_after=None):
        super().__init__(message)
        self.code=code if type(code) is int and 100<=code<=599 else None
        self.retry_after=retry_after if type(retry_after) is int and 0<=retry_after<=2**31-1 else None


def _response_error(body,response,message):
    code=body.get('error_code') if isinstance(body,dict) else None
    if type(code) is not int or not 100<=code<=599:code=getattr(response,'status_code',None)
    # Only a Telegram API rejection can carry a safe resubmission delay. An
    # HTTP error page or a network exception leaves delivery outcome unknown.
    retry_after=None
    if isinstance(body,dict) and body.get('ok') is False and body.get('error_code')==429:
        parameters=body.get('parameters')
        if isinstance(parameters,dict):retry_after=parameters.get('retry_after')
    return TelegramError(message,code=code,retry_after=retry_after)


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
        if not isinstance(body,dict):raise _response_error(body,response,'Telegram вернул некорректный ответ.')
        if body.get('ok') is not True:
            if method in ('editMessageText','editMessageReplyMarkup') and isinstance(body.get('description'),str) and 'message is not modified' in body['description']:
                return None
            error=_response_error(body,response,'Telegram не принял '+method+'.')
            if error.code is not None:error.args=('Telegram не принял '+method+' (код '+str(error.code)+').',)
            raise error
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

    def _audio_retry(self,request):
        for attempt in range(3):
            try:return request()
            except TelegramError as error:
                # Explicit 429 means Telegram rejected the method. Retrying a
                # timeout could deliver the same audio twice, so never do that.
                if attempt==2 or error.code!=429 or error.retry_after is None or error.retry_after>60:raise
                time.sleep(error.retry_after)

    def document(self,chat_id,file,caption,keyboard=None):
        file=Path(file)
        fields={'chat_id':str(chat_id),'caption':caption[:1024]}
        if keyboard:fields['reply_markup']=json.dumps({'inline_keyboard':keyboard},ensure_ascii=False)
        try:
            with file.open('rb') as stream:
                encoder=MultipartEncoder(fields={**fields,'document':(file.name,stream,'application/vnd.android.package-archive')})
                response=self.session.post(self.base+'sendDocument',data=encoder,headers={'Content-Type':encoder.content_type},timeout=(15,90));body=response.json()
        except Exception:raise TelegramError('Не удалось отправить установочный файл.') from None
        if not isinstance(body,dict) or body.get('ok') is not True:raise _response_error(body,response,'Telegram не принял установочный файл.')
        return body.get('result')

    def audio(self,chat_id,result,keyboard=None):
        track=result['track']
        custom=result.get('caption')
        caption=(custom if isinstance(custom,str) else track['artist']+' — '+track['title'])[:1000]
        if keyboard is None:keyboard=[[{'text':'Открыть на Диске','url':result['disk_url']},
                                       {'text':'Источник','url':track['url']}]]
        keyboard={'inline_keyboard':keyboard}
        fields={'chat_id':str(chat_id),'caption':caption,'performer':track['artist'][:64],
                'title':track['title'][:64],'duration':str(round(result['duration'])),
                'reply_markup':json.dumps(keyboard,ensure_ascii=False)}
        if result.get('telegram_file_id'):
            return self._audio_retry(lambda:self.call('sendAudio',{**fields,'audio':result['telegram_file_id']},timeout=90))
        local=Path(result['local_path'])
        def upload():
            try:
                # Multipart streams are consumed by each request. Reopen both
                # the file and encoder after an explicit, retryable rejection.
                with local.open('rb') as file:
                    attempt_fields={**fields,'audio':(local.name,file,'audio/mpeg' if local.suffix=='.mp3' else 'audio/mp4')}
                    stream=MultipartEncoder(fields=attempt_fields)
                    response=self.session.post(self.base+'sendAudio',data=stream,headers={'Content-Type':stream.content_type},timeout=(15,180))
                    body=response.json()
            except Exception:
                raise TelegramError('Аудио сохранено на Диск, но Telegram не принял файл.') from None
            if not isinstance(body,dict) or body.get('ok') is not True:
                raise _response_error(body,response,'Аудио сохранено на Диск, но Telegram не принял файл.')
            if 'result' not in body:raise TelegramError('Telegram вернул некорректный ответ на отправку аудио.')
            return body['result']
        return self._audio_retry(upload)
