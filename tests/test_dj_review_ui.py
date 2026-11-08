"""Owner DJ-point QA through real bot routing and durable worker jobs."""
import json

import pytest

from aidj import dj_review as R,worker
from aidj.bot import configure_profile,handle_update
from aidj.dj_review_ui import DjReviewUI,caption,keyboard,process
from aidj.store import claim_job,enqueue,meta_get
from tests.test_playlists import env,update


@pytest.fixture
def setup(env,tmp_path,monkeypatch):
    settings,db,disk,bot,manager,result=env
    from aidj.web_catalog import sync_assets
    playlist=manager.create('Volleyball');manager.add(playlist['id'],result,tmp_path);sync_assets(db)
    asset=db.execute('SELECT * FROM web_assets WHERE md5=?',(result['md5'],)).fetchone()
    graph={'version':1,'wordGuarded':True,'bpm':120,'entries':[[0,.7],[50,.8],[70,.9]],'exits':[[80,1,.8],[100,1,.8]]}
    acoustic={**graph,'quality':'acoustic','wordGuarded':False,'needsReview':True,'entries':[[0,.7],[30,.8],[60,.8]],'exits':[[40,.8,.7],[90,.8,.7]]}
    payload={'version':3,'cueSchema':4,'anchors':graph,'acousticAnchors':acoustic,'cues':[],'remix':[],'needsReview':True,'reviewRecommended':True}
    db.execute('INSERT OR REPLACE INTO dj_analysis VALUES (?,?,?,?,?)',(result['md5'],asset['id'],'complete',json.dumps(payload),0));db.commit()
    uploads=[];rendered=[]
    def render(settings,db,card,work,disk=None):
        rendered.append(dict(card));file=work/'point.m4a';file.write_bytes(b'point-audio')
        start_file=work/'from-point.m4a';start_file.write_bytes(b'from-point')
        beginning={'track':result['track'],'local_path':str(start_file),'duration':5,'md5':'start-md5','bytes':10}
        return {'track':result['track'],'local_path':str(file),'duration':10,'md5':'point-md5','bytes':11,'fromPoint':beginning}
    def audio(chat,value,keys=None):
        assert value['duration'] in (5,10) and value['local_path']
        uploads.append((chat,dict(value),keys));return {'message_id':800+len(uploads),'audio':{'file_id':'point-file-'+str(len(uploads))}}
    monkeypatch.setattr(R,'render_clip',render);bot.audio=audio
    return settings,db,disk,bot,manager,result,uploads,rendered


def start(setup,n=1):
    settings,db,disk,bot,*_=setup
    handle_update(db,{},bot,update(text='/djreview',n=n),settings)
    job=claim_job(db);assert job and json.loads(job['payload'])['client']=='dj-review'
    assert worker.run(job['id'])==0
    return R.owned_card(db,json.loads(job['payload'])['card_id'],11,22)


def test_owner_gets_context_and_exact_point_preview_and_vote_never_auto_advances(setup):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    card=start(setup)
    assert card['state']=='open' and card['quality']=='acoustic'
    assert len(uploads)==2 and len(rendered)==1 and all(upload[0]==22 for upload in uploads)
    from aidj.dj_review_ui import timestamp
    assert 'Точка на 0:05' in uploads[0][1]['caption'] and 'В оригинале: '+timestamp(card['position']) in uploads[0][1]['caption']
    assert 'вход' in uploads[0][1]['caption'] and uploads[0][2]==[]
    assert 'С опорной точки · 0:00' in uploads[1][1]['caption'] and uploads[1][1]['duration']==5
    assert {b['text'] for row in uploads[1][2] for b in row}=={'Хорошо','Плохо','Комментарий','Следующая','Закончить'}
    assert card['message_id']==802 and card['telegram_file_id']=='point-file-2'
    record=json.loads(db.execute('SELECT result FROM jobs').fetchone()[0])
    assert record['context_message_id']==801 and record['message_id']==802
    handle_update(db,{},bot,update(data=f'djrv:vote:{card["id"]}:1',n=2),settings)
    assert R.owned_card(db,card['id'],11,22)['state']=='voted'
    assert db.execute('SELECT value FROM dj_boundary_votes').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1 and len(uploads)==2
    assert '✓ Хорошо' in str(bot.markups)
    # Repeated delivery cannot enqueue another clip or change another point.
    handle_update(db,{},bot,update(data=f'djrv:vote:{card["id"]}:1',n=2),settings)
    assert db.execute('SELECT COUNT(*) FROM dj_boundary_votes').fetchone()[0]==1
    handle_update(db,{},bot,update(data=f'djrv:next:{card["id"]}',n=3),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2


def test_comment_is_explicit_and_can_still_bind_the_previous_voted_card(setup):
    settings,db,disk,bot,*_=setup
    first=start(setup)
    handle_update(db,{},bot,update(data=f'djrv:vote:{first["id"]}:-1',n=2),settings)
    handle_update(db,{},bot,update(data=f'djrv:next:{first["id"]}',n=3),settings)
    handle_update(db,{},bot,update(data=f'djrv:comment:{first["id"]}',n=4),settings)
    assert R.pending_comment(db,11,22)['id']==first['id']
    handle_update(db,{},bot,update(text='Слово обрезалось в конце',n=5),settings)
    comment=db.execute('SELECT * FROM dj_boundary_comments').fetchone()
    assert comment['point_id']==first['point_id'] and comment['text']=='Слово обрезалось в конце'
    assert R.pending_comment(db,11,22) is None
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2  # Comment never becomes a music request.


def test_pending_comment_does_not_trap_owner_commands_or_public_suggestion_flow(setup):
    settings,db,disk,bot,*_=setup
    card=start(setup)
    handle_update(db,{},bot,update(data=f'djrv:comment:{card["id"]}',n=2),settings)
    assert R.pending_comment(db,11,22)
    handle_update(db,{},bot,update(text='/help',n=3),settings)
    assert R.pending_comment(db,11,22) is None and 'Проверить DJ-точки' in str(bot.sent[-1])
    handle_update(db,{},bot,update(data=f'djrv:comment:{card["id"]}',n=4),settings)
    handle_update(db,{},bot,update(text='/cancel',n=5),settings)
    assert R.pending_comment(db,11,22) is None and 'отменён' in bot.sent[-1][1]
    assert R.owned_card(db,card['id'],11,22)['state']=='open'
    # Existing listener-review controls still work without touching DJ votes.
    handle_update(db,{},bot,update(text='/review',uid=101,n=6),settings)
    assert 'Начать размечать' in str(bot.sent[-1])
    assert 'Проверить DJ-точки' not in str(bot.sent[-1])


def test_repeated_start_keeps_one_preparing_card_and_finishing_prevents_late_audio(setup):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    for n in (1,2,3):handle_update(db,{},bot,update(text='/djreview',n=n),settings)
    assert db.execute('SELECT COUNT(*) FROM dj_boundary_cards').fetchone()[0]==1
    job=claim_job(db);assert job
    handle_update(db,{},bot,update(data='djrv:finish',n=4),settings)
    assert worker.run(job['id'])==0
    assert not uploads and not rendered
    assert not db.execute("SELECT 1 FROM dj_boundary_cards WHERE state IN ('preparing','open')").fetchone()
    assert 'закончена' in bot.sent[-1][1]


def test_finish_received_during_render_also_prevents_sending(setup,monkeypatch):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    def stop_in_render(settings,db,card,work,disk=None):
        R.finish_session(db,11,22)
        return {'track':result['track'],'local_path':'unused','duration':10}
    monkeypatch.setattr(R,'render_clip',stop_in_render)
    assert process(settings,db,job,bot)==0 and not uploads


@pytest.mark.parametrize('uid,cid,private',[(101,22,True),(11,101,True),(11,-22,False)])
def test_guests_and_other_chats_cannot_use_private_callbacks_or_jobs(setup,uid,cid,private):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    card=start(setup);before=len(uploads)
    for n,data in enumerate(['djrv:start',f'djrv:vote:{card["id"]}:-1',f'djrv:comment:{card["id"]}',f'djrv:next:{card["id"]}','djrv:finish'],2):
        event=update(data=data,uid=uid,n=n);event['callback_query']['message']['chat']={'id':cid,'type':'private' if private else 'group'}
        handle_update(db,{},bot,event,settings)
    assert not db.execute('SELECT 1 FROM dj_boundary_votes').fetchone()
    assert R.owned_card(db,card['id'],11,22)['state']=='open'
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1 and len(uploads)==before
    # Even a forged durable worker payload must recheck owner+chat before IO.
    enqueue(db,'forged-dj',cid,uid,0,'forged',{'client':'dj-review','card_id':card['id']})
    job=claim_job(db);assert job
    assert worker.run(job['id'])==1 and len(uploads)==before and len(rendered)==1


def test_dj_commands_and_menu_are_visible_only_in_owner_scope(setup):
    settings,db,disk,bot,*_=setup
    configure_profile(db,bot)
    registrations=[data for method,data in bot.answered if method=='setMyCommands']
    assert all(c['command']!='djreview' for data in registrations if not data.get('scope') for c in data['commands'])
    scoped=next(data for data in registrations if data.get('scope'))
    assert scoped['scope']['chat_id']==22 and any(c['command']=='djreview' for c in scoped['commands'])
    assert meta_get(db,'profile_version')=='8'
    handle_update(db,{},bot,update(text='/help'),settings)
    assert 'Проверить DJ-точки' in str(bot.sent[-1])
    handle_update(db,{},bot,update(text='/djreview',uid=101,n=2),settings)
    assert 'владельцу' in bot.sent[-1][1] and not db.execute('SELECT 1 FROM jobs').fetchone()


def test_caption_does_not_replace_song_metadata_and_supports_both_roles():
    card={'title':'Title','artist':'Artist','roles':['entry','exit'],'position':63.25,'quality':'word'}
    value=caption(card)
    assert 'Точка на 0:05' in value and 'вход/выход' in value and '1:03.2' in value
    assert card['title']=='Title' and card['artist']=='Artist'
    assert len(caption({**card,'title':'x'*3000}))<=1000


def test_telegram_audio_custom_caption_is_plain_bounded_and_preserves_default_artist_title():
    from aidj.telegram import Telegram
    calls=[];bot=Telegram.__new__(Telegram)
    bot.call=lambda method,data,**kwargs:calls.append((method,data)) or {'message_id':1}
    original={'track':{'artist':'Artist','title':'Title'},'duration':10,'telegram_file_id':'cached-point'}
    keys=[[{'text':'Хорошо','callback_data':'djrv:test'}]]
    bot.audio(22,original,keys)
    assert calls[-1][1]['caption']=='Artist — Title'
    bot.audio(22,{**original,'caption':'Точка на 0:05\n'+'x'*1200},keys)
    fields=calls[-1][1]
    assert fields['caption'].startswith('Точка на 0:05\n') and len(fields['caption'])==1000
    assert fields['performer']=='Artist' and fields['title']=='Title' and 'parse_mode' not in fields
    bot.audio(22,{**original,'caption':123},keys)
    assert calls[-1][1]['caption']=='Artist — Title'
    bot.audio(22,original,[])
    assert json.loads(calls[-1][1]['reply_markup'])=={'inline_keyboard':[]}
    defaults={**original,'disk_url':'https://example.invalid/disk','track':{**original['track'],'url':'https://example.invalid/source'}}
    bot.audio(22,defaults,None)
    assert len(json.loads(calls[-1][1]['reply_markup'])['inline_keyboard'][0])==2


def test_normal_owner_link_is_a_music_request_until_comment_is_explicitly_requested(setup):
    settings,db,disk,bot,*_=setup
    card=start(setup)
    handle_update(db,{},bot,update(text='https://www.youtube.com/watch?v=abcdefghijk',n=2),settings)
    jobs=db.execute('SELECT payload FROM jobs ORDER BY id').fetchall()
    assert len(jobs)==2 and json.loads(jobs[-1][0]).get('client')!='dj-review'
    assert not db.execute('SELECT 1 FROM dj_boundary_comments').fetchone()
    handle_update(db,{},bot,update(data=f'djrv:comment:{card["id"]}',n=3),settings)
    handle_update(db,{},bot,update(text='https://example.invalid/ — вот здесь заметил обрыв',n=4),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
    assert 'https://example.invalid/' in db.execute('SELECT text FROM dj_boundary_comments').fetchone()[0]


def test_guest_text_cannot_consume_the_owners_pending_comment(setup):
    settings,db,disk,bot,*_=setup
    card=start(setup)
    handle_update(db,{},bot,update(data=f'djrv:comment:{card["id"]}',n=2),settings)
    guest=update(text='Гость предлагает новую песню',uid=101,n=3);guest['message']['chat']['id']=101
    handle_update(db,{},bot,guest,settings)
    assert R.pending_comment(db,11,22)['id']==card['id']
    assert not db.execute('SELECT 1 FROM dj_boundary_comments').fetchone()
    handle_update(db,{},bot,update(text='Мой комментарий',n=4),settings)
    row=db.execute('SELECT * FROM dj_boundary_comments').fetchone()
    assert row['user_id']==11 and row['chat_id']==22 and row['text']=='Мой комментарий'


def test_finish_during_failed_render_does_not_send_a_late_error(setup,monkeypatch):
    settings,db,disk,bot,*_=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    before=len(bot.sent)
    def render_then_fail(settings,db,card,work,disk=None):
        R.finish_session(db,11,22)
        raise R.ReviewError('render stopped')
    monkeypatch.setattr(R,'render_clip',render_then_fail)
    assert process(settings,db,job,bot)==0 and len(bot.sent)==before


def fail_first_comment_ack(bot,monkeypatch):
    from aidj.telegram import TelegramError
    original=bot.message;failed=[]
    def send(chat,text,keys=None):
        if text=='Комментарий сохранён к этой точке.' and not failed:
            failed.append(True);raise TelegramError('temporary acknowledgement failure')
        return original(chat,text,keys)
    monkeypatch.setattr(bot,'message',send)
    return failed


def test_comment_replay_after_failed_ack_is_consumed_without_creating_music_job(setup,monkeypatch):
    from aidj.telegram import TelegramError
    settings,db,disk,bot,*_=setup
    card=start(setup)
    handle_update(db,{},bot,update(data=f'djrv:comment:{card["id"]}',n=2),settings)
    failed=fail_first_comment_ack(bot,monkeypatch)
    event=update(text='Слово оборвалось на этой точке',n=3)
    with pytest.raises(TelegramError):handle_update(db,{},bot,event,settings)
    assert failed and R.pending_comment(db,11,22) is None
    assert db.execute('SELECT COUNT(*) FROM dj_boundary_comments').fetchone()[0]==1
    handle_update(db,{},bot,event,settings)
    assert db.execute('SELECT COUNT(*) FROM dj_boundary_comments').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    # A different update is still a normal owner music request, not swallowed.
    handle_update(db,{},bot,update(text='https://www.youtube.com/watch?v=abcdefghijk',n=4),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
    assert json.loads(db.execute('SELECT payload FROM jobs ORDER BY id DESC LIMIT 1').fetchone()[0]).get('client')!='dj-review'


def test_replayed_old_comment_cannot_consume_or_clear_a_new_points_pending_comment(setup,monkeypatch):
    from aidj.telegram import TelegramError
    settings,db,disk,bot,*_=setup
    first=start(setup)
    handle_update(db,{},bot,update(data=f'djrv:comment:{first["id"]}',n=2),settings)
    fail_first_comment_ack(bot,monkeypatch)
    old=update(text='Первый комментарий',n=3)
    with pytest.raises(TelegramError):handle_update(db,{},bot,old,settings)
    handle_update(db,{},bot,update(data=f'djrv:vote:{first["id"]}:1',n=4),settings)
    handle_update(db,{},bot,update(data=f'djrv:next:{first["id"]}',n=5),settings)
    job=claim_job(db);assert job and worker.run(job['id'])==0
    second=R.owned_card(db,json.loads(job['payload'])['card_id'],11,22)
    handle_update(db,{},bot,update(data=f'djrv:comment:{second["id"]}',n=6),settings)
    assert R.pending_comment(db,11,22)['id']==second['id']
    handle_update(db,{},bot,old,settings)
    assert R.pending_comment(db,11,22)['id']==second['id']
    assert db.execute('SELECT COUNT(*) FROM dj_boundary_comments').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
    handle_update(db,{},bot,update(text='Новый комментарий ко второй точке',n=7),settings)
    rows=db.execute('SELECT point_id,text FROM dj_boundary_comments ORDER BY id').fetchall()
    assert [(row['point_id'],row['text']) for row in rows]==[(first['point_id'],'Первый комментарий'),(second['point_id'],'Новый комментарий ко второй точке')]
    assert R.pending_comment(db,11,22) is None


def test_finish_after_context_upload_prevents_second_audio_and_buttons(setup,monkeypatch):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    original=bot.audio
    def finish_after_first(chat,value,keys=None):
        sent=original(chat,value,keys)
        if len(uploads)==1:R.finish_session(db,11,22)
        return sent
    monkeypatch.setattr(bot,'audio',finish_after_first)
    assert process(settings,db,job,bot)==0
    assert len(uploads)==1 and uploads[0][2]==[]
    assert db.execute('SELECT state FROM dj_boundary_cards').fetchone()[0]=='closed'


def test_second_audio_failure_does_not_mark_an_incomplete_pair_open_or_completed(setup,monkeypatch):
    from aidj.telegram import TelegramError
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    original=bot.audio
    def fail_second(chat,value,keys=None):
        if value['duration']==5:raise TelegramError('second upload failed')
        return original(chat,value,keys)
    monkeypatch.setattr(bot,'audio',fail_second)
    assert process(settings,db,job,bot)==1
    assert len(uploads)==1 and uploads[0][2]==[]
    assert db.execute('SELECT state FROM dj_boundary_cards').fetchone()[0]=='failed'
    saved=db.execute('SELECT status,result,error FROM jobs').fetchone()
    details=json.loads(saved['result'])
    assert saved['status']=='failed' and details['context_message_id']==801
    assert details['phase']=='send_from_point' and details['exception_type']=='TelegramError'
    assert details['card_id']==json.loads(job['payload'])['card_id']
    assert 'second upload failed' not in saved['result']+saved['error']
    assert 'Telegram' in saved['error'] and 'Повторить' in saved['error'] and 'Диске' not in saved['error']
    assert 'Повторить' in str(bot.sent[-1])
    assert not db.execute('SELECT 1 FROM dj_boundary_votes').fetchone()


def test_first_audio_failure_has_durable_safe_phase_without_a_receipt(setup,monkeypatch):
    from aidj.telegram import TelegramError
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    secret='https://private.invalid/download?token=must-not-persist'
    def fail_upload(chat,value,keys=None):raise TelegramError(secret,code=429,retry_after=3)
    monkeypatch.setattr(bot,'audio',fail_upload)
    assert process(settings,db,job,bot)==1 and not uploads
    saved=db.execute('SELECT status,result,error FROM jobs').fetchone()
    assert saved['status']=='failed'
    assert json.loads(saved['result'])=={'card_id':json.loads(job['payload'])['card_id'],'phase':'send_context','exception_type':'TelegramError','api_code':429,'retry_after':3}
    assert secret not in saved['result']+saved['error']+str(bot.sent)
    assert 'Telegram' in saved['error'] and 'Повторить' in saved['error'] and 'Диске' not in saved['error']
    assert R.owned_card(db,json.loads(job['payload'])['card_id'],11,22)['state']=='failed'


def test_renderer_failure_has_durable_safe_phase_and_no_upload(setup,monkeypatch):
    settings,db,disk,bot,manager,result,uploads,rendered=setup
    handle_update(db,{},bot,update(text='/djreview'),settings);job=claim_job(db)
    def fail_render(*args,**kwargs):raise OSError('private filesystem details')
    monkeypatch.setattr(R,'render_clip',fail_render)
    assert process(settings,db,job,bot)==1 and not uploads
    saved=db.execute('SELECT status,result,error FROM jobs').fetchone()
    assert saved['status']=='failed'
    assert json.loads(saved['result'])=={'card_id':json.loads(job['payload'])['card_id'],'phase':'render','exception_type':'OSError'}
    assert 'private filesystem details' not in saved['result']+saved['error']+str(bot.sent)
