"""Owner-only listening QA for individual DJ boundary points."""
import json
import math
from pathlib import Path
import tempfile

from .locking import worker_lock
from .review_ui import owner
from .store import job_update
from .telegram import TelegramError


def keyboard(card_id,value=None):
    return [[{'text':('✓ ' if value==vote else '')+label,'callback_data':f'djrv:vote:{card_id}:{vote}'}
             for vote,label in ((1,'Хорошо'),(-1,'Плохо'))],
            [{'text':'Комментарий','callback_data':'djrv:comment:'+card_id},
             {'text':'Следующая','callback_data':'djrv:next:'+card_id}],
            [{'text':'Закончить','callback_data':'djrv:finish'}]]


def timestamp(seconds):
    value=float(seconds)
    if not math.isfinite(value) or value<0:value=0
    minutes=int(value//60);part=value%60
    return f'{minutes}:{part:04.1f}'


def caption(card,from_point=False):
    roles='/'.join({'entry':'вход','exit':'выход'}[role] for role in card.get('roles',[]) if role in ('entry','exit')) or 'граница фрагмента'
    position=card.get('position',card.get('point_ms',0)/1000)
    text=(card.get('artist','')+' — '+card.get('title',''))[:600]
    label='С опорной точки · 0:00' if from_point else 'Точка на 0:05'
    text+='\n\n'+label+' · '+roles+'\nВ оригинале: '+timestamp(position)
    if card.get('quality')=='acoustic':text+='\nНужна проверка на слух.'
    return text[:1000]


class DjReviewUI:
    def __init__(self,db,bot,settings):self.db,self.bot,self.settings=db,bot,settings
    @property
    def core(self):
        from . import dj_review
        return dj_review

    def start(self,event,uid,chat):
        R=self.core;R.ensure_owner(self.db,uid,chat);R.cancel_comment(self.db,uid,chat)
        self.db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(uid,));self.db.commit()
        self.bot.message(chat,'Для каждой DJ-точки пришлю два аудио: 10 секунд контекста с границей на 0:05 и ещё 5 секунд прямо с неё. Оцени, не режется ли слово и удачно ли выбрано место. Кнопки — под вторым аудио; после оценки можно оставить комментарий и нажать «Следующая».',
                         [[{'text':'Закончить','callback_data':'djrv:finish'}]])
        self.next(event,uid,chat)

    def next(self,event,uid,chat):
        R=self.core;R.ensure_owner(self.db,uid,chat)
        card,queued=R.next_card(self.db,event,uid,chat)
        if not card:
            self.bot.message(chat,'Все доступные DJ-точки проверены. Новые появятся после анализа песен.',
                             [[{'text':'Проверить ещё','callback_data':'djrv:start'},{'text':'Закончить','callback_data':'djrv:finish'}]])
        elif not queued:
            if card['state']=='open':self.bot.message(chat,'Оба аудио уже в чате. Оцени точку или нажми «Следующая».',keyboard(card['id']))
            else:self.bot.message(chat,'Один фрагмент уже готовится. Дождись его или нажми «Закончить».',[[{'text':'Закончить','callback_data':'djrv:finish'}]])

    def message(self,update):
        message=update.get('message') or {};chat=message.get('chat') or {};person=message.get('from') or {}
        uid=person.get('id');cid=chat.get('id');text=(message.get('text') or '').strip()
        command=text.split(' ',1)[0].split('@',1)[0]
        explicit=command=='/djreview' or text=='Проверить DJ-точки'
        if chat.get('type')!='private' or not uid or person.get('is_bot'):return False
        if not owner(self.db,uid,cid):
            if explicit:self.bot.message(cid,'Проверка DJ-точек доступна владельцу.');return True
            return False
        R=self.core
        # A committed comment remains consumed even if Telegram's acknowledgement
        # failed and the poller delivers the same update again. Check before the
        # current prompt so an old replay cannot consume a newer point's input.
        if R.comment_handled(self.db,update.get('update_id'),uid,cid):return True
        pending=R.pending_comment(self.db,uid,cid)
        try:
            if explicit:self.start(update['update_id'],uid,cid);return True
            if text.startswith('/'):
                if pending:R.cancel_comment(self.db,uid,cid)
                if pending and command=='/cancel':self.bot.message(cid,'Ввод комментария отменён.');return True
                return False
            if not pending:return False
            if not text:self.bot.message(cid,'Пришли комментарий текстом. /cancel — отменить ввод.');return True
            R.comment(self.db,pending['id'],uid,cid,text,event=update['update_id'])
            self.bot.message(cid,'Комментарий сохранён к этой точке.',keyboard(pending['id']))
        except R.ReviewError as exc:self.bot.message(cid,str(exc))
        return True

    def callback(self,update):
        cb=update.get('callback_query') or {};parts=cb.get('data','').split(':')
        if not parts or parts[0]!='djrv':return False
        message=cb.get('message') or {};chat=message.get('chat') or {};person=cb.get('from') or {}
        uid=person.get('id');cid=chat.get('id');answered=False
        def answer(text=''):
            nonlocal answered
            if answered:return
            answered=True
            try:self.bot.call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':text[:180]})
            except TelegramError:pass
        def markup(card,value=None):
            if card.get('message_id'):
                try:self.bot.markup(cid,card['message_id'],keyboard(card['id'],value))
                except TelegramError:pass
        if chat.get('type')!='private' or not uid or person.get('is_bot') or not owner(self.db,uid,cid):
            answer('Проверка DJ-точек доступна владельцу.');return True
        R=self.core
        try:
            R.ensure_owner(self.db,uid,cid);action=parts[1] if len(parts)>1 else ''
            if action=='start' and len(parts)==2:
                answer('Готовлю точку');self.start(update['update_id'],uid,cid)
            elif action=='vote' and len(parts)==4:
                value=int(parts[3])
                if value not in (-1,1):raise ValueError()
                card=R.vote(self.db,parts[2],uid,cid,value);answer('Оценка сохранена');markup(card,value)
            elif action=='comment' and len(parts)==3:
                R.prompt_comment(self.db,parts[2],uid,cid);answer()
                self.bot.message(cid,'Напиши комментарий к этой точке. Он сохранится вместе с оценкой.',
                                 [[{'text':'Отмена','callback_data':'djrv:cancel:'+parts[2]}]])
            elif action=='cancel' and len(parts)==3:
                R.owned_card(self.db,parts[2],uid,cid);R.cancel_comment(self.db,uid,cid);answer('Ввод отменён')
            elif action=='next' and len(parts)==3:
                card=R.owned_card(self.db,parts[2],uid,cid)
                if card['state']=='open':R.skip(self.db,card['id'],uid,cid)
                elif card['state'] not in ('voted','skipped','failed'):raise R.ReviewError('Дождись текущего фрагмента.')
                answer('Готовлю следующую точку');self.next(update['update_id'],uid,cid)
            elif action=='finish' and len(parts)==2:
                R.finish_session(self.db,uid,cid);answer('Проверка закончена')
                self.bot.message(cid,'Проверка DJ-точек закончена. Оценки и комментарии сохранены.')
            else:answer('Эта кнопка недоступна. Открой /djreview.')
        except (R.ReviewError,ValueError,IndexError) as exc:
            text=str(exc) if isinstance(exc,R.ReviewError) else 'Эта кнопка недоступна. Открой /djreview.'
            if answered:self.bot.message(cid,text)
            else:answer(text)
        return True


def process(settings,db,row,bot,disk=None):
    """Render one queued owner point, serialized with other audio workers."""
    from . import dj_review as R
    payload=json.loads(row['payload']);card_id=payload.get('card_id');job_id=row['id']
    phase='render';context_message_id=None
    def diagnostics(exception=None):
        value={'card_id':card_id,'phase':phase}
        if context_message_id is not None:value['context_message_id']=context_message_id
        if exception is not None:value['exception_type']=type(exception).__name__[:100]
        return json.dumps(value)
    try:
        R.ensure_owner(db,row['user_id'],row['chat_id'])
        card=R.owned_card(db,card_id,row['user_id'],row['chat_id'])
        if card['state']!='preparing':
            job_update(db,job_id,status='completed',stage='Проверка уже закончена');return 0
        job_update(db,job_id,stage='Готовлю DJ-точку',result=diagnostics())
        with worker_lock(settings),tempfile.TemporaryDirectory(prefix=f'dj-review-{job_id}-',dir=settings.state/'work') as folder:
            result=R.render_clip(settings,db,card,Path(folder),disk=disk)
            # A finish/cancel received while rendering must prevent late audio.
            R.ensure_owner(db,row['user_id'],row['chat_id'])
            card=R.owned_card(db,card_id,row['user_id'],row['chat_id'])
            if card['state']!='preparing':
                job_update(db,job_id,status='completed',stage='Проверка уже закончена');return 0
            from_point=result.get('fromPoint')
            if not isinstance(from_point,dict):raise R.ReviewError('Не подготовлено аудио с опорной точки. Нажми /djreview, чтобы повторить.')
            context={key:value for key,value in result.items() if key!='fromPoint'}
            context['caption']=caption(card)
            phase='send_context'
            job_update(db,job_id,stage='Отправляю контекст точки',result=diagnostics())
            context_sent=bot.audio(row['chat_id'],context,[])
            context_message_id=context_sent['message_id']
            phase='send_from_point'
            job_update(db,job_id,stage='Отправляю начало с точки',result=diagnostics())
            # A finish/owner change after the context recording must prevent
            # the second upload and must not expose a vote on an incomplete pair.
            R.ensure_owner(db,row['user_id'],row['chat_id'])
            card=R.owned_card(db,card_id,row['user_id'],row['chat_id'])
            if card['state']!='preparing':
                job_update(db,job_id,status='completed',stage='Проверка уже закончена');return 0
            sent=bot.audio(row['chat_id'],{**from_point,'caption':caption(card,from_point=True)},keyboard(card_id))
            R.mark_open(db,card_id,row['user_id'],row['chat_id'],sent['message_id'],sent.get('audio',{}).get('file_id'))
        job_update(db,job_id,status='completed',stage='Точка готова',result=json.dumps({'card_id':card_id,'message_id':sent['message_id'],'context_message_id':context_sent['message_id']}));return 0
    except Exception as exc:
        # Cancellation can arrive during the download/transcode itself. A late
        # renderer failure must not reopen the session or post after Finish.
        if owner(db,row['user_id'],row['chat_id']):
            try:current=R.owned_card(db,card_id,row['user_id'],row['chat_id'])
            except R.ReviewError:current=None
            if current and current['state'] in ('closed','skipped','voted','open'):
                job_update(db,job_id,status='completed',stage='Проверка уже закончена');return 0
        try:R.mark_failed(db,card_id,row['user_id'],row['chat_id'])
        except R.ReviewError:pass
        text=('Не удалось отправить аудио проверки DJ-точки в Telegram. Нажми «Повторить».'
              if isinstance(exc,TelegramError) and phase.startswith('send_')
              else 'Не удалось подготовить проверку DJ-точки. Нажми «Повторить».')
        # Persist only the class and bounded workflow identifiers. Exception
        # messages may contain private download URLs or Telegram credentials.
        job_update(db,job_id,status='failed',stage='Ошибка DJ-проверки',error=text,result=diagnostics(exc))
        # Re-check the destination even on an error: a forged queued job cannot
        # make the bot send any response or private clip to a guest chat.
        if owner(db,row['user_id'],row['chat_id']):
            try:bot.message(row['chat_id'],text,[[{'text':'Повторить','callback_data':'djrv:start'}]])
            except TelegramError:pass
        print('DJ_REVIEW_JOB_FAILED',job_id,type(exc).__name__,flush=True);return 1
