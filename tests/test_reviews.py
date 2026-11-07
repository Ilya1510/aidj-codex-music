import hashlib
import json
from pathlib import Path

import pytest

from aidj import reviews as R,review_worker,worker
from aidj.bot import handle_update
from aidj.pipeline import SelectionNeeded
from aidj.review_ui import ReviewUI
from aidj.sources import Track
from aidj.store import claim_job,enqueue,job_update
from aidj.web_catalog import catalog,sync_assets
from tests.test_playlists import env


def update(text=None,data=None,uid=101,n=1):
    message={'chat':{'id':uid if uid!=11 else 22,'type':'private'},'from':{'id':uid},'message_id':50}
    if data:return {'update_id':n,'callback_query':{'id':str(n),'from':{'id':uid},'message':message,'data':data}}
    return {'update_id':n,'message':{**message,'text':text}}


@pytest.fixture
def setup(env,tmp_path):
    settings,db,disk,bot,manager,result=env
    playlist=manager.create('Volleyball');manager.add(playlist['id'],result,tmp_path)
    uploads=[];downloads=[]
    def download(remote,local,digest=None):
        data=disk.files[remote]
        assert not digest or hashlib.md5(data).hexdigest()==digest
        Path(local).write_bytes(data);downloads.append(remote)
    disk.download=download
    def audio(chat,result,keyboard):
        uploads.append((chat,dict(result),keyboard))
        return {'message_id':700+len(uploads),'audio':{'file_id':'telegram-stored'}}
    bot.audio=audio
    return settings,db,disk,bot,manager,result,playlist,uploads,downloads


def process(setup):
    settings,db,disk,bot,*_=setup
    job=claim_job(db);assert job
    return review_worker.process(settings,db,job,bot,disk=disk)


def start(setup,uid=101,n=1):
    settings,db,disk,bot,*_=setup
    handle_update(db,{},bot,update(text='/start review',uid=uid,n=n),settings)
    handle_update(db,{},bot,update(data='rv:next',uid=uid,n=n+1),settings)


def test_guest_link_menu_audio_vote_auto_next_and_durable_unique_votes(setup):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    start(setup);assert 'Начать размечать' in str(bot.sent)
    assert process(setup)==0
    card=db.execute('SELECT * FROM review_cards').fetchone()
    assert card['state']=='open' and len(downloads)==1
    # No private Disk/app controls or URLs in a guest audio keyboard.
    keyboard=uploads[0][2]
    assert all(b['callback_data'].startswith('rv:') and 'url' not in b for row in keyboard for b in row)
    handle_update(db,{},bot,update(data=f'rv:vote:{card["id"]}:-1',n=3),settings)
    handle_update(db,{},bot,update(data=f'rv:vote:{card["id"]}:-1',n=3),settings)
    assert db.execute('SELECT COUNT(*) FROM review_votes').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    # An old card allows correcting a vote without scheduling another song.
    handle_update(db,{},bot,update(data=f'rv:vote:{card["id"]}:1',n=4),settings)
    assert db.execute('SELECT value FROM review_votes').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    assert 'Все доступные' in bot.sent[-1][1]


def test_other_listener_reuses_telegram_audio_and_has_independent_vote(setup):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    start(setup);assert process(setup)==0
    first=db.execute('SELECT * FROM review_cards').fetchone()
    start(setup,uid=202,n=10);assert process(setup)==0
    assert len(downloads)==1 and uploads[-1][1]['telegram_file_id']=='telegram-stored'
    second=db.execute('SELECT * FROM review_cards WHERE user_id=202').fetchone()
    assert second['item_id']==first['item_id']
    R.vote(db,first['id'],101,101,-1);R.vote(db,second['id'],202,202,0)
    stats=R.summary(db,p['id']);assert stats[0]['dislikes']==1 and stats[0]['neutral']==1


def test_auto_next_never_resends_rated_song(setup,tmp_path,monkeypatch):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    more={**result,'track':{**result['track'],'id':'456','title':'Other'},'md5':'other'}
    disk.files['/Музыка/aidj_codex_music/other.mp3']=b'other-audio'
    more.update(remote_path='/Музыка/aidj_codex_music/other.mp3',md5=hashlib.md5(b'other-audio').hexdigest(),bytes=11)
    manager.add(p['id'],more,tmp_path);pool=R.playlist_items(db,p['id'])
    for uid in (303,404):
        db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],pool[0],uid,1,0))
    monkeypatch.setattr(R.random,'choices',lambda population,weights,k:[population[weights.index(max(weights))]])
    db.commit();start(setup);process(setup)
    card=db.execute("SELECT * FROM review_cards WHERE state='open'").fetchone();assert card['item_id']==pool[1]
    handle_update(db,{},bot,update(data=f'rv:vote:{card["id"]}:0',n=3),settings)
    assert process(setup)==0
    next_=db.execute("SELECT * FROM review_cards WHERE state='open'").fetchone()
    assert next_['item_id']==pool[0]


def test_guests_cannot_vote_for_others_or_call_any_owner_actions(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    for n,data in enumerate([f'rv:vote:{card["id"]}:1',f'rv:remove:{card["item_id"]}',f'rv:open:{card["item_id"]}',
        f'rv:archive:{card["item_id"]}','rv:stats:0','rv:proposals:0','rv:share','pd:fake','pc:fake','pl:fake:1','pick:fake:0'],20):
        handle_update(db,{},bot,update(data=data,uid=202,n=n),settings)
    assert not db.execute('SELECT 1 FROM review_votes').fetchone()
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    assert manager.contains(p['id'],result)
    for n,command in enumerate(['/app','/android','/newplaylist Hacked','/status','/reviewstats'],40):
        handle_update(db,{},bot,update(text=command,uid=202,n=n),settings)
    assert db.execute('SELECT COUNT(*) FROM playlists').fetchone()[0]==1
    assert 'Подключить приложение' not in str(bot.sent) and 'Файл на Диске' not in str(bot.sent)


def test_exact_link_preview_uses_disk_without_youtube_and_cannot_repropose(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    def forbidden(*a,**kw):raise AssertionError('must not query source')
    monkeypatch.setattr(review_worker,'Pipeline',forbidden)
    handle_update(db,{},bot,update(text='/suggest https://music.yandex.ru/track/123'),settings)
    assert process(setup)==0
    assert 'уже есть в плейлисте' in bot.edited[-1][2]
    proposal=db.execute('SELECT * FROM review_proposals').fetchone()
    assert proposal['status']=='preview'
    assert 'rv:confirm:' not in str(uploads[-1][2])
    row,accepted=R.confirm(db,proposal['id'],101,101);assert not accepted
    assert manager.list()[0]['tracks']==1


def test_suggestion_is_private_until_confirmation_and_not_added_to_library(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    new={**result,'track':{**result['track'],'id':'999','title':'New song'},'remote_path':'/Музыка/_aidj_suggestions/new.m4a',
        'md5':'new-song','bytes':10,'telegram_file_id':'new-telegram'}
    class Pipeline:
        def __init__(self,settings,db,**kwargs):
            assert settings.disk_folder=='/Музыка/_aidj_suggestions' and kwargs['namespace']=='review:'
        def select(self,*a):return Track.from_dict(new['track'])
        def run(self,*a):
            db.execute('INSERT INTO cache VALUES (?,?,?,?)',('review:test',json.dumps(new),'new-telegram',0));db.commit();return new
    monkeypatch.setattr(review_worker,'Pipeline',Pipeline)
    handle_update(db,{},bot,update(text='/suggest Artist New song'),settings)
    assert process(setup)==0
    proposal=db.execute('SELECT * FROM review_proposals').fetchone()
    assert proposal['status']=='preview'
    assert proposal['item_id'] not in R.playlist_items(db,p['id'])
    assert len(catalog(db)['tracks'])==1
    handle_update(db,{},bot,update(data='rv:confirm:'+proposal['id'],n=2),settings)
    assert proposal['item_id'] in R.playlist_items(db,p['id'])
    assert len(catalog(db)['tracks'])==1 and manager.list()[0]['tracks']==1
    assert db.execute('SELECT COUNT(*) FROM app_deliveries').fetchone()[0]==0
    # Same user's repeated confirm is one proposal, not another vote or copy.
    handle_update(db,{},bot,update(data='rv:confirm:'+proposal['id'],n=2),settings)
    assert db.execute('SELECT COUNT(*) FROM review_proposals').fetchone()[0]==1
    assert len(R.summary(db,p['id'],True))==1


def test_metadata_similarity_never_blocks_another_version(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    other={**result,'track':{**result['track'],'source':'youtube','id':'abcdefghijk','title':'Song (Official Audio)'},'md5':'different'}
    found=R.matches(db,p['id'],other)
    assert found and not found[0]['exact']
    item=R.remember(db,other);R.ensure_user(db,101,101)
    proposal=R.proposal(db,101,101,p['id'],item)
    assert R.confirm(db,proposal['id'],101,101)[1]
    same_hash={**other,'md5':result['md5']}
    assert R.matches(db,p['id'],same_hash)[0]['exact']
    db.execute('INSERT INTO playlist_aliases VALUES (?,?,?)',(p['id'],'youtube:alias123456','yandex:123'));db.commit()
    alias={**other,'track':{**other['track'],'id':'alias123456'}}
    assert R.matches(db,p['id'],alias)[0]['exact']


def test_suggestion_choices_are_scoped_to_author_and_stay_in_review_worker(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    class Pipeline:
        def __init__(self,*a,**kw):pass
        def select(self,*a):raise SelectionNeeded('query',[Track('youtube','abcdefghijk','Title','Artist','https://youtube.com/watch?v=abcdefghijk')])
    monkeypatch.setattr(review_worker,'Pipeline',Pipeline)
    handle_update(db,{},bot,update(text='/suggest query'),settings);assert process(setup)==0
    choice=db.execute('SELECT * FROM review_choices').fetchone()
    handle_update(db,{},bot,update(data=f'rv:pick:{choice["id"]}:0',uid=202,n=2),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    handle_update(db,{},bot,update(data=f'rv:pick:{choice["id"]}:0',n=3),settings)
    handle_update(db,{},bot,update(data=f'rv:pick:{choice["id"]}:0',n=3),settings)
    jobs=db.execute('SELECT * FROM jobs ORDER BY id').fetchall();assert len(jobs)==2
    payload=json.loads(jobs[-1]['payload']);assert payload['client']=='review' and payload['track']['id']=='abcdefghijk'
    assert not db.execute('SELECT 1 FROM playlist_selections').fetchone()


def test_failed_delivery_releases_card_and_request_limits_prevent_duplicate_work(setup):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    start(setup)
    handle_update(db,{},bot,update(data='rv:next',n=2),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    def fail(*a,**kw):raise RuntimeError('PRIVATE_PROVIDER_SECRET')
    bot.audio=fail
    assert process(setup)==1
    assert db.execute('SELECT state FROM review_cards').fetchone()[0]=='failed'
    assert 'PRIVATE_PROVIDER_SECRET' not in str(bot.sent)
    handle_update(db,{},bot,update(data='rv:next',n=3),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2


def test_owner_gets_stats_listening_and_existing_one_click_playlist_controls(setup):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone();R.vote(db,card['id'],101,101,-1)
    handle_update(db,{},bot,update(text='/reviewstats',uid=11,n=30),settings)
    assert '1 оценок от 1' in bot.sent[-1][1] and '👎 1' in bot.sent[-1][1]
    handle_update(db,{},bot,update(data='rv:open:'+card['item_id'],uid=11,n=31),settings)
    assert process(setup)==0
    keyboard=uploads[-1][2]
    assert '✓ Volleyball' in str(keyboard) and 'Скачать в приложение' in str(keyboard)
    assert 'Убрать из плейлиста' in str(keyboard)
    assert len(downloads)==1


def test_worker_rechecks_owner_even_if_private_job_is_forged(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    R.ensure_user(db,101,101);item=R.playlist_items(db,p['id'])[0]
    enqueue(db,'forged',101,101,0,'remove',{'client':'review','action':'remove','playlist_id':p['id'],'item_id':item})
    assert process(setup)==1 and manager.contains(p['id'],result)


def test_guest_cannot_select_an_unshared_private_playlist(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    private=manager.create('Личное')
    handle_update(db,{},bot,update(text='/review'),settings)
    handle_update(db,{},bot,update(data='rv:choose',n=2),settings)
    assert 'Личное' not in str(bot.sent[-1])
    handle_update(db,{},bot,update(data='rv:playlist:'+str(private['id']),n=3),settings)
    assert db.execute('SELECT playlist_id FROM review_users WHERE user_id=101').fetchone()[0]==p['id']


def test_owner_removes_only_selected_song_and_keeps_file_and_votes(setup):
    settings,db,disk,bot,manager,result,p,uploads,downloads=setup
    def move(source,target):disk.files[target]=disk.files.pop(source)
    disk.move=move
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    R.vote(db,card['id'],101,101,-1)
    handle_update(db,{},bot,update(data='rv:remove:'+card['item_id'],uid=11,n=30),settings)
    assert process(setup)==0
    assert not manager.contains(p['id'],result)
    assert db.execute('SELECT value FROM review_votes').fetchone()[0]==-1
    assert any(path.startswith('/Музыка/_aidj_removed/') for path in disk.files)
    assert card['item_id'] not in R.playlist_items(db,p['id'])


def test_worker_dispatch_uses_public_job_handler_not_owner_pipeline(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);job=claim_job(db)
    monkeypatch.setattr(worker,'Pipeline',lambda *a:pytest.fail('owner pipeline must not run'))
    monkeypatch.setattr(review_worker,'Disk',lambda *a:disk)
    assert worker.run(job['id'])==0
    assert not db.execute('SELECT 1 FROM playlist_selections').fetchone()


def test_skipping_does_not_count_as_neutral_or_repeat_the_track(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    handle_update(db,{},bot,update(data='rv:skip:'+card['id'],n=3),settings)
    assert not db.execute('SELECT 1 FROM review_votes').fetchone()
    assert db.execute('SELECT state FROM review_cards').fetchone()[0]=='skipped'
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1


def test_vote_survives_failure_to_update_old_message_or_enqueue_next(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    # The DB is the source of truth even when the keyboard is no longer editable.
    from aidj.telegram import TelegramError
    def fail(*a):raise TelegramError('Telegram не принял editMessageReplyMarkup (код 400).')
    bot.markup=fail
    handle_update(db,{},bot,update(data='rv:vote:'+card['id']+':1',n=3),settings)
    assert 'Все доступные' in bot.sent[-1][1]
    assert db.execute('SELECT value FROM review_votes').fetchone()[0]==1


def test_similar_multiword_title_with_uploader_name_is_an_advisory_match(setup,tmp_path):
    settings,db,disk,bot,manager,result,p,*_=setup
    changed={**result,'track':{**result['track'],'title':'Piece of Me','artist':'Britney Spears'}}
    db.execute("UPDATE playlist_tracks SET label='Britney Spears — Piece of Me' WHERE playlist_id=?",(p['id'],));db.commit()
    found=R.matches(db,p['id'],{**changed,'md5':'different','track':{**changed['track'],'source':'youtube','id':'abcdefghijk','artist':'Some Uploader','title':'Piece of Me (Official Audio)'}})
    assert found and not found[0]['exact']


def test_guest_bad_url_is_handled_without_blocking_update_poller(setup):
    settings,db,disk,bot,*_=setup
    handle_update(db,{},bot,update(text='/suggest https://youtube.com:bad/watch?v=abcdefghijk'),settings)
    assert 'Некорректная ссылка' in bot.sent[-1][1]
    assert not db.execute('SELECT 1 FROM jobs').fetchone()


def test_profile_update_failure_is_nonfatal_and_success_has_separate_guest_commands(setup):
    settings,db,disk,bot,*_=setup
    from aidj.bot import configure_profile
    from aidj.telegram import TelegramError
    from aidj.store import meta_get
    class Fail:
        def call(self,*a,**kw):raise TelegramError('temporary network error')
    assert configure_profile(db,Fail()) is False
    assert meta_get(db,'profile_version')!='7'
    assert configure_profile(db,bot) is True
    assert meta_get(db,'profile_version')=='7'
    commands=[data for method,data in bot.answered if method=='setMyCommands']
    assert len(commands)==2
    assert [c['command'] for c in commands[0]['commands']]==['review','suggest','help']
    assert commands[1]['scope']=={'type':'chat','chat_id':22}
    assert 'reviewstats' in [c['command'] for c in commands[1]['commands']]


def test_real_pipeline_fallback_cache_for_proposals_stays_out_of_app_catalog(tmp_path):
    from aidj.config import Settings
    from aidj.store import connect
    from aidj.pipeline import Pipeline
    from tests.test_pipeline import Sources,Disk
    settings=Settings(state=tmp_path/'state',disk_folder='/Музыка/_aidj_suggestions')
    db=connect(settings);sources=Sources();disk=Disk(tmp_path)
    pipeline=Pipeline(settings,db,sources,disk,namespace='review:')
    for name in ('first','second'):
        work=tmp_path/name;work.mkdir()
        result=pipeline.run('https://music.yandex.ru/track/123',{},work)
        assert result['cache_key'].startswith('review:')
        assert result['remote_path'].startswith('/Музыка/_aidj_suggestions/')
    assert result['cached'] and sources.youtube_downloads==1
    assert catalog(db)['tracks']==[]


def test_direct_friend_link_becomes_preview_request_not_playlist_mutation(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    handle_update(db,{},bot,update(text='https://music.yandex.ru/track/123'),settings)
    row=db.execute('SELECT * FROM jobs').fetchone();assert row
    assert json.loads(row['payload'])['client']=='review'
    assert json.loads(row['payload'])['action']=='suggest'
    assert process(setup)==0
    assert manager.list()[0]['tracks']==1
    assert not db.execute('SELECT 1 FROM playlist_selections').fetchone()
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()


def test_owner_sees_nickname_per_vote_and_proposal_but_guests_do_not(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    R.remember_profile(db,{'id':101,'username':'friend_one','first_name':'Friend'})
    R.vote(db,card['id'],101,101,-1)
    R.ensure_user(db,202,202);R.remember_profile(db,{'id':202,'first_name':'Саша'})
    R.proposal(db,202,202,p['id'],card['item_id'])
    db.execute("UPDATE review_proposals SET status='confirmed' WHERE user_id=202");db.commit()
    rows=R.people(db,p['id'],card['item_id'])
    assert {R.person_label(row) for row in rows}=={'@friend_one','Саша'}
    handle_update(db,{},bot,update(text='/reviewstats',uid=11,n=30),settings)
    assert '@friend_one' in bot.sent[-1][1]
    handle_update(db,{},bot,update(data='rv:people:'+card['item_id']+':0',uid=11,n=31),settings)
    assert '@friend_one · 👎' in bot.sent[-1][1]
    assert 'Саша · Пока без оценки · предложил(а)' in bot.sent[-1][1]
    before=len(bot.sent)
    handle_update(db,{},bot,update(data='rv:people:'+card['item_id']+':0',uid=303,n=32),settings)
    assert len(bot.sent)==before
    # Current Telegram metadata can update without losing past votes.
    R.remember_profile(db,{'id':101,'username':'new_name','first_name':'Friend'})
    assert any(R.person_label(row)=='@new_name' and row['value']==-1 for row in R.people(db,p['id'],card['item_id']))

def test_review_dislike_bonus_is_fixed_and_above_unrated():
    assert R.review_weight(0,0)==1
    assert R.review_weight(1,1)==1.25
    assert R.review_weight(100,100)==1.25
    assert R.review_weight(1,0)<1


def test_review_selection_shares_alias_votes_and_uses_weights(setup,tmp_path,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    more={**result,'track':{**result['track'],'id':'456','title':'Unrated'},
          'md5':hashlib.md5(b'other-audio').hexdigest(),'bytes':11,'remote_path':'/Музыка/aidj_codex_music/other.mp3'}
    disk.files[more['remote_path']]=b'other-audio';manager.add(p['id'],more,tmp_path)
    original=R.remember(db,result)
    alias=R.remember(db,{**result,'track':{**result['track'],'id':'alias'}})
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],original,303,1,1))
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],alias,303,-1,2));db.commit()
    seen=[]
    def choose(population,weights,k):
        seen.append(dict(zip(population,weights)))
        return [population[weights.index(max(weights))]]
    monkeypatch.setattr(R.random,'choices',choose)
    card,_=R.next_card(db,'weighted',101,101)
    assert json.loads(db.execute('SELECT result FROM review_items WHERE id=?',(card['item_id'],)).fetchone()[0])['md5']==result['md5']
    assert sorted(seen[0].values())==[1,1.25]
    # A vote for a different content alias excludes all copies for this listener.
    db.execute("UPDATE jobs SET status='completed'")
    db.execute("UPDATE review_cards SET state='voted'")
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],alias,101,0,3));db.commit()
    next_card,_=R.next_card(db,'after-alias',101,101)
    assert next_card['item_id']==R.remember(db,more)


def test_review_skipped_alias_is_not_offered_again(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    alias=R.remember(db,{**result,'track':{**result['track'],'id':'alias'}})
    R.ensure_user(db,101,101)
    db.execute("INSERT INTO review_cards(id,user_id,chat_id,playlist_id,item_id,state,created) VALUES ('skip',101,101,?,?,'skipped',0)",(p['id'],alias));db.commit()
    assert R.next_card(db,'after-skip',101,101)==(None,False)


def test_unrated_suggestions_are_strictly_first_then_join_normal_library_selection(setup,monkeypatch):
    settings,db,disk,bot,manager,result,p,*_=setup
    def suggest(key,digest):
        value={**result,'track':{**result['track'],'id':key,'title':key},'md5':digest}
        identity=R.remember(db,value);proposal=R.proposal(db,202,202,p['id'],identity)
        db.execute("UPDATE review_proposals SET status='confirmed' WHERE id=?",(proposal['id'],));db.commit()
        return identity
    fresh=suggest('fresh','f'*32);rated=suggest('rated','e'*32)
    original=R.remember(db,result)
    # Neutral counts as an existing assessment; the old dislike bonus cannot
    # overtake a globally unassessed suggestion.
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],rated,202,0,1))
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],original,303,-1,1));db.commit()
    offered=[]
    def choose(population,weights,k):
        offered.append(list(population));return [fresh if fresh in population else rated if rated in population else population[-1]]
    monkeypatch.setattr(R.random,'choices',choose)
    for n,expected in enumerate((fresh,rated,original)):
        card,_=R.next_card(db,'priority:'+str(n),101,101)
        if n==0:assert offered[-1]==[fresh]
        elif n==1:assert set(offered[-1])=={rated,original}
        else:assert offered[-1]==[original]
        assert card['item_id']==expected
        db.execute("UPDATE jobs SET status='completed'")
        db.execute("UPDATE review_cards SET state='open' WHERE id=?",(card['id'],));db.commit()
        R.vote(db,card['id'],101,101,1)
    assert R.next_card(db,'priority:finished',101,101)==(None,False)


@pytest.mark.parametrize('foreign_vote',[None,-1,0,1])
def test_author_never_receives_own_suggestion_even_through_a_library_alias(setup,foreign_vote):
    settings,db,disk,bot,manager,result,p,*_=setup
    original=R.remember(db,result)
    alias=R.remember(db,{**result,'track':{**result['track'],'source':'youtube','id':'self-alias'}})
    proposed=R.proposal(db,101,101,p['id'],alias)
    db.execute("UPDATE review_proposals SET status='confirmed' WHERE id=?",(proposed['id'],))
    if foreign_vote is not None:db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],original,202,foreign_vote,1))
    db.commit()
    assert R.next_card(db,'self-excluded',101,101)==(None,False)
    # Archiving the suggestion does not make its accepted library copy fair
    # game for automatic self-review later.
    db.execute("UPDATE review_proposals SET status='archived' WHERE id=?",(proposed['id'],));db.commit()
    assert R.next_card(db,'self-archived',101,101)==(None,False)
    other={**result,'track':{**result['track'],'id':'somebody-else'},'md5':'b'*32}
    item=R.remember(db,other);proposal=R.proposal(db,202,202,p['id'],item)
    db.execute("UPDATE review_proposals SET status='confirmed' WHERE id=?",(proposal['id'],));db.commit()
    assert R.next_card(db,'other-author',101,101)[0]['item_id']==item
