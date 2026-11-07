"""Telegram listening and suggestions UI, isolated from owner-only controls."""
import json
import time

from . import reviews as R
from .sources import SourceError
from .store import meta_get
from .telegram import TelegramError


def owner(db,user_id,chat_id):
    return meta_get(db,'owner_id')==str(user_id) and meta_get(db,'owner_chat_id')==str(chat_id)


def vote_keyboard(card_id,value=None):
    return [[{'text':('✓ ' if value==v else '')+label,'callback_data':f'rv:vote:{card_id}:{v}'}
        for v,label in [(1,'👍'),(-1,'👎'),(0,'Всё равно')]],
        [{'text':'Пропустить','callback_data':'rv:skip:'+card_id},{'text':'Меню','callback_data':'rv:menu'}]]


class ReviewUI:
    def __init__(self,db,bot,settings):self.db,self.bot,self.settings=db,bot,settings

    def menu(self,user_id,chat_id):
        user=R.ensure_user(self.db,user_id,chat_id)
        name=self.db.execute('SELECT name FROM playlists WHERE id=?',(user['playlist_id'],)).fetchone()[0]
        count=self.db.execute('SELECT COUNT(*) FROM review_votes WHERE playlist_id=? AND user_id=?',(user['playlist_id'],user_id)).fetchone()[0]
        keyboard=[[{'text':'▶ Начать размечать','callback_data':'rv:next'}],
            [{'text':'＋ Предложить песню','callback_data':'rv:suggest'}]]
        if owner(self.db,user_id,chat_id):
            if self.db.execute('SELECT COUNT(*) FROM playlists').fetchone()[0]>1:
                keyboard.append([{'text':'Выбрать плейлист','callback_data':'rv:choose'}])
            keyboard.extend([[{'text':'Оценки','callback_data':'rv:stats:0'},{'text':'Предложения','callback_data':'rv:proposals:0'}],
                [{'text':'Ссылка друзьям','callback_data':'rv:share'}],
                [{'text':'Проверить DJ-точки','callback_data':'djrv:start'}]])
        self.bot.message(chat_id,f'Разметка · {name}\n\nСлушай песню и оцени, подходит ли она для этого плейлиста: 👍 / 👎 / «Всё равно». После оценки придёт следующая.\n\nТвоих оценок: {count}. Можно предложить свою песню — сначала послушаешь и подтвердишь запись.',keyboard)

    def prompt(self,user_id,chat_id):
        R.ensure_user(self.db,user_id,chat_id)
        self.db.execute("UPDATE review_users SET mode='suggest' WHERE user_id=?",(user_id,));self.db.commit()
        self.bot.message(chat_id,'Пришли исполнителя и название или ссылку на песню Яндекс Музыки / YouTube. Сначала проверю совпадения в плейлисте и пришлю запись послушать.',[[{'text':'Отмена','callback_data':'rv:menu'}]])

    def next(self,event,user_id,chat_id):
        card,queued=R.next_card(self.db,event,user_id,chat_id)
        self.db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(user_id,));self.db.commit()
        if card and not queued:
            if card['state']=='open':self.bot.message(chat_id,'Предыдущая песня уже в чате — оцени её или пропусти.',vote_keyboard(card['id']))
        elif not card:self.bot.message(chat_id,'Все доступные песни уже разобраны. Новые предложения и песни появятся здесь позже.',[[{'text':'Предложить песню','callback_data':'rv:suggest'},{'text':'Меню','callback_data':'rv:menu'}]])

    def message(self,update,is_owner):
        message=update.get('message') or {};text=(message.get('text') or '').strip()
        uid=message['from']['id'];chat=message['chat']['id'];command=text.split(' ',1)[0].split('@',1)[0]
        user=self.db.execute('SELECT * FROM review_users WHERE user_id=?',(uid,)).fetchone()
        explicit=command in ('/review','/suggest','/reviewstats','/suggestions','/reviewlink') or (command=='/start' and text.partition(' ')[2].strip()=='review')
        pending=user and user['mode']=='suggest' and text and not text.startswith('/')
        if is_owner and not explicit and not pending and not (command=='/cancel' and user and user['mode']=='suggest'):return False
        R.remember_profile(self.db,message['from'])
        try:
            if command in ('/reviewstats','/suggestions','/reviewlink'):
                if not is_owner:raise R.ReviewError('Эта команда доступна владельцу.')
                R.ensure_user(self.db,uid,chat)
                if command=='/reviewlink':self.share(chat)
                else:self.report(uid,chat,0,command=='/suggestions')
            elif command=='/suggest':
                query=text.partition(' ')[2].strip()
                if query:self.submit(update['update_id'],uid,chat,query)
                else:self.prompt(uid,chat)
            elif pending or (not is_owner and text and not text.startswith('/')):self.submit(update['update_id'],uid,chat,text)
            else:
                if user:self.db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(uid,));self.db.commit()
                self.menu(uid,chat)
        except (R.ReviewError,SourceError) as exc:self.bot.message(chat,str(exc))
        return True

    def submit(self,event,uid,chat,text):
        previous=self.db.execute('SELECT 1 FROM jobs WHERE update_id=?',('review:'+str(event),)).fetchone()
        if previous:return
        R.capacity(self.db,uid)
        # Validate before sending a progress message; keep the suggestion prompt on error.
        from .sources import parse_input
        parse_input(text)
        sent=self.bot.message(chat,'Проверяю плейлист и ищу запись…')
        try:R.submit_suggestion(self.db,event,uid,chat,text,sent['message_id'])
        except (R.ReviewError,SourceError) as exc:self.bot.edit(chat,sent['message_id'],str(exc))

    def share(self,chat):
        self.bot.message(chat,'Ссылка для друзей:\nhttps://t.me/aidj_codex_music_bot?start=review\n\nОткрой бота и нажми «Начать размечать».')

    def report(self,uid,chat,page=0,proposals=False):
        if not owner(self.db,uid,chat):raise R.ReviewError('Оценки и предложения доступны владельцу.')
        user=R.ensure_user(self.db,uid,chat);pid=user['playlist_id']
        rows=R.summary(self.db,pid,proposals);page=max(0,min(page,max(0,(len(rows)-1)//8)))
        total=self.db.execute('SELECT COUNT(*),COUNT(DISTINCT user_id) FROM review_votes WHERE playlist_id=?',(pid,)).fetchone()
        name=self.db.execute('SELECT name FROM playlists WHERE id=?',(pid,)).fetchone()[0]
        text=('Предложенные песни' if proposals else 'Оценки песен')+' · '+name+f'\n{total[0]} оценок от {total[1]} участников.\n'
        text+='Подтверждённые предложения сохраняются в «Предложке» и видны в приложении. Скачивание на телефон и добавление в общий плейлист — по твоему выбору.' if proposals else 'Сверху — песни с наибольшим числом дизлайков. Смотри и число голосов: одна оценка ещё мало что говорит.'
        keyboard=[]
        for row in rows[page*8:page*8+8]:
            t=json.loads(row['result'])['track'];label=t['artist']+' — '+t['title']
            text+=f'\n\n{label[:170]}\n👍 {row["likes"]} · 👎 {row["dislikes"]} · Всё равно {row["neutral"]}'
            participants=R.people(self.db,pid,row['id'])
            disliked=[R.person_label(p)[:40] for p in participants if p['value']==-1]
            proposed=[R.person_label(p)[:40] for p in participants if p['proposed']]
            if disliked:text+='\n👎: '+', '.join(disliked[:3])+('…' if len(disliked)>3 else '')
            if proposals and proposed:text+='\nПредложили: '+', '.join(proposed[:3])+('…' if len(proposed)>3 else '')
            keyboard.append([{'text':('Послушать: '+label)[:45],'callback_data':'rv:open:'+row['id']},
                {'text':'Кто оценил','callback_data':'rv:people:'+row['id']+':0'}])
        if not rows:text+='\n\nПока пусто.'
        prefix='rv:proposals:' if proposals else 'rv:stats:'
        nav=[]
        if page:nav.append({'text':'←','callback_data':prefix+str(page-1)})
        if (page+1)*8<len(rows):nav.append({'text':'→','callback_data':prefix+str(page+1)})
        if nav:keyboard.append(nav)
        keyboard.append([{'text':'Меню разметки','callback_data':'rv:menu'}])
        self.bot.message(chat,text,keyboard)

    def people(self,uid,chat,item,page=0):
        if not owner(self.db,uid,chat):raise R.ReviewError('Эта информация доступна владельцу.')
        user=R.ensure_user(self.db,uid,chat);result=R.item_result(self.db,item)
        rows=R.people(self.db,user['playlist_id'],item);page=max(0,min(page,max(0,(len(rows)-1)//12)))
        track=result['track'];text=track['artist']+' — '+track['title']+'\n\nКто оценил и предложил:\n'
        for row in rows[page*12:page*12+12]:
            rating={1:'👍',-1:'👎',0:'Всё равно'}.get(row['value'],'Пока без оценки')
            text+='\n'+R.person_label(row)+' · '+rating+(' · предложил(а)' if row['proposed'] else '')
        if not rows:text+='\nОценок и подтверждённых предложений пока нет.'
        navigation=[];prefix='rv:people:'+item+':'
        if page:navigation.append({'text':'←','callback_data':prefix+str(page-1)})
        if (page+1)*12<len(rows):navigation.append({'text':'→','callback_data':prefix+str(page+1)})
        keyboard=([navigation] if navigation else [])+[[{'text':'Послушать','callback_data':'rv:open:'+item},
            {'text':'Оценки','callback_data':'rv:stats:0'}]]
        self.bot.message(chat,text,keyboard)

    def callback(self,update):
        cb=update.get('callback_query') or {};parts=cb.get('data','').split(':')
        if not parts or parts[0]!='rv':return False
        message=cb.get('message') or {};chat=message.get('chat') or {};user=cb.get('from') or {}
        uid=user.get('id');cid=chat.get('id')
        answered=False
        def answer(text=''):
            nonlocal answered
            if answered:return
            try:self.bot.call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':text[:180]})
            except TelegramError:pass
            answered=True
        def markup(message_id,keyboard):
            try:self.bot.markup(cid,message_id,keyboard)
            except TelegramError:pass
        if chat.get('type')!='private' or not uid or user.get('is_bot'):
            answer('Открой бота в личном чате.');return True
        try:
            R.remember_profile(self.db,user)
            R.ensure_user(self.db,uid,cid)
            action=parts[1] if len(parts)>1 else ''
            if action in ('stats','proposals','people','open','remove','archive','share') and not owner(self.db,uid,cid):raise R.ReviewError('Эта кнопка доступна владельцу.')
            # Acknowledge before catalog reads or audio work; all mutations remain scoped by user.
            if action=='vote' and len(parts)==4:
                card,fresh=R.vote(self.db,parts[2],uid,cid,int(parts[3]));answer('Оценка сохранена')
                if card['message_id']:markup(card['message_id'],vote_keyboard(card['id'],int(parts[3])))
                if fresh:
                    try:self.next(str(update['update_id'])+':next',uid,cid)
                    except R.ReviewError as exc:self.bot.message(cid,str(exc),[[{'text':'Следующая песня','callback_data':'rv:next'}]])
            elif action=='skip' and len(parts)==3:
                card=self.db.execute("SELECT * FROM review_cards WHERE id=? AND user_id=? AND chat_id=? AND state='open'",(parts[2],uid,cid)).fetchone()
                if not card:raise R.ReviewError('Эта песня уже разобрана.')
                self.db.execute("UPDATE review_cards SET state='skipped' WHERE id=?",(card['id'],));self.db.commit();answer('Пропущено')
                if card['message_id']:markup(card['message_id'],[[{'text':'Меню','callback_data':'rv:menu'}]])
                self.next(str(update['update_id'])+':next',uid,cid)
            elif action=='confirm' and len(parts)==3:
                row,accepted=R.confirm(self.db,parts[2],uid,cid);answer('Отправлено на разметку' if accepted else 'Эта песня уже есть в плейлисте')
                markup(message['message_id'],[[{'text':'✓ Отправлено на разметку' if accepted else 'Уже в плейлисте','callback_data':'rv:menu'}],
                    [{'text':'Начать размечать','callback_data':'rv:next'},{'text':'Предложить ещё','callback_data':'rv:suggest'}]])
            elif action in ('pick','yt'):
                choice=self.db.execute('SELECT * FROM review_choices WHERE id=? AND user_id=? AND chat_id=?',(parts[2],uid,cid)).fetchone()
                if not choice or choice['used'] or time.time()-choice['created']>3600:raise R.ReviewError('Выбор устарел. Предложи песню заново.')
                payload={'action':'suggest','playlist_id':choice['playlist_id']}
                if action=='pick':
                    index=int(parts[3]);tracks=json.loads(choice['tracks'])
                    if index<0 or index>=len(tracks):raise R.ReviewError('Выбери запись из списка.')
                    payload['track']=tracks[index]
                else:payload['youtube_only']=True
                R.add_job(self.db,update['update_id'],uid,cid,choice['query'],payload,message['message_id'])
                self.db.execute('UPDATE review_choices SET used=1 WHERE id=?',(choice['id'],));self.db.commit();answer('Готовлю запись')
            elif action=='playlist' and len(parts)==3:
                pid=int(parts[2])
                if not owner(self.db,uid,cid) and pid!=R.shared_playlist(self.db):raise R.ReviewError('Этот плейлист не открыт для разметки.')
                if not self.db.execute('SELECT 1 FROM playlists WHERE id=?',(pid,)).fetchone():raise R.ReviewError('Плейлист не найден.')
                if R.busy(self.db,uid):raise R.ReviewError('Сначала дождись текущей песни.')
                self.db.execute("UPDATE review_cards SET state='cancelled' WHERE user_id=? AND state IN ('queued','open')",(uid,))
                self.db.execute("UPDATE review_users SET playlist_id=?,mode='menu' WHERE user_id=?",(pid,uid));self.db.commit();answer();self.menu(uid,cid)
            elif action=='choose':
                answer()
                playlists=self.db.execute('SELECT id,name FROM playlists WHERE ? OR id=?',(owner(self.db,uid,cid),R.shared_playlist(self.db)))
                self.bot.message(cid,'Какой плейлист размечаем?',[[{'text':p['name'],'callback_data':'rv:playlist:'+str(p['id'])}] for p in playlists])
            elif action in ('stats','proposals'):
                answer();self.report(uid,cid,int(parts[2]),action=='proposals')
            elif action=='people' and len(parts)==4:
                answer();self.people(uid,cid,parts[2],int(parts[3]))
            elif action=='share':answer();self.share(cid)
            elif action in ('open','remove','archive') and len(parts)==3:
                item=parts[2];person=R.ensure_user(self.db,uid,cid);pid=person['playlist_id']
                if item not in R.playlist_items(self.db,pid):raise R.ReviewError('Песни больше нет в этой разметке.')
                if action=='archive':
                    self.db.execute("UPDATE review_proposals SET status='archived' WHERE playlist_id=? AND item_id=?",(pid,item));self.db.commit();answer('Предложение снято с разметки')
                    markup(message['message_id'],[[{'text':'Предложения','callback_data':'rv:proposals:0'}]])
                else:
                    R.add_job(self.db,update['update_id'],uid,cid,'Просмотр песни' if action=='open' else 'Убрать из плейлиста',
                        {'action':'inspect' if action=='open' else 'remove','playlist_id':pid,'item_id':item},0)
                    answer('Готовлю песню' if action=='open' else 'Убираю из плейлиста')
            elif action=='next':answer();self.next(update['update_id'],uid,cid)
            elif action=='suggest':answer();self.prompt(uid,cid)
            elif action=='menu':
                self.db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(uid,));self.db.commit();answer();self.menu(uid,cid)
            else:answer('Открой /review заново.')
        except (R.ReviewError,SourceError,ValueError,IndexError) as exc:
            # Never echo a malformed callback (it is controlled by the sender).
            text=str(exc) if isinstance(exc,(R.ReviewError,SourceError)) else 'Кнопка недоступна. Открой /review.'
            if answered:self.bot.message(cid,text,[[{'text':'Меню','callback_data':'rv:menu'}]])
            else:answer(text)
        return True
