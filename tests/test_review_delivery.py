import hashlib
import json

from aidj import reviews as R
from aidj.review_delivery import schedule_saves,process_save,accepted,deliver_notices,suggestions_playlist
from aidj.store import claim_job,job_update
from aidj.telegram import TelegramError
from aidj.web_catalog import catalog,mutate,revision
from tests.test_playlists import env
from tests.test_reviews import setup


def proposed(setup,uid=101,item=None):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    R.ensure_user(db,uid,uid)
    if item is None:
        body=b'new suggested audio'
        result={**original,'track':{**original['track'],'id':'999','title':'Suggestion'},
            'remote_path':'/Музыка/_aidj_suggestions/new.mp3','bytes':len(body),'md5':hashlib.md5(body).hexdigest()}
        disk.files[result['remote_path']]=body
        item=R.remember(db,result)
    proposal=R.proposal(db,uid,uid,playlist['id'],item)
    assert R.confirm(db,proposal['id'],uid,uid)[1]
    return item,proposal


def save_queued(setup):
    settings,db,disk,bot,*_=setup
    row=claim_job(db);assert row
    assert json.loads(row['payload'])['action']=='save'
    return process_save(settings,db,row,disk)


def test_confirmed_proposal_is_saved_and_visible_without_automatic_phone_download(setup):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,p=proposed(setup)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()
    assert save_queued(setup)==0
    suggestions=suggestions_playlist(settings,db,disk)
    result=R.item_result(db,item)
    assert suggestions['name']=='Предложка' and manager.contains(suggestions['id'],result)
    assert not manager.contains(playlist['id'],result)
    assert len(catalog(db)['tracks'])==2
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()
    copies=disk.copies
    R.confirm(db,p['id'],101,101);schedule_saves(db)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1 and disk.copies==copies
    assert db.execute('SELECT state FROM review_saves').fetchone()[0]=='saved'
    assert not bot.sent and not db.execute('SELECT 1 FROM review_notices').fetchone()


def test_multiple_proposers_share_one_copy_and_each_gets_one_acceptance_notice(setup,tmp_path):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,one=proposed(setup,101);_,two=proposed(setup,202,item)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    assert save_queued(setup)==0
    assert db.execute('SELECT COUNT(*) FROM app_deliveries').fetchone()[0]==0
    result=R.item_result(db,item);manager.add(playlist['id'],result,tmp_path)
    manager.add(playlist['id'],result,tmp_path)
    assert db.execute('SELECT COUNT(*) FROM review_notices').fetchone()[0]==2
    assert deliver_notices(db,bot)==2
    assert {row[0] for row in bot.sent}=={101,202}
    assert all('Поздравляю!' in row[1] and 'общий плейлист «Volleyball»' in row[1] for row in bot.sent)
    assert deliver_notices(db,bot)==0 and len(bot.sent)==2


def test_web_playlist_addition_also_sends_the_author_notification(setup,tmp_path):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup);save_queued(setup)
    mutate(settings,db,disk,{'opId':'native-owner-add','action':'add','playlistId':playlist['id'],
        'trackId':item,'revision':revision(db,playlist['id'])},tmp_path)
    assert deliver_notices(db,bot)==1 and bot.sent[-1][0]==101


def test_failed_disk_copy_retries_without_phone_publish_or_false_congratulations(setup,monkeypatch):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup)
    copy=disk.copy
    def fail(*a):raise RuntimeError('PRIVATE_PROVIDER_EXCEPTION')
    disk.copy=fail
    assert save_queued(setup)==1
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()
    assert not db.execute('SELECT 1 FROM review_notices').fetchone()
    assert db.execute('SELECT state FROM review_saves').fetchone()[0]=='failed'
    disk.copy=copy
    db.execute('UPDATE review_saves SET retry_at=0');db.commit()
    assert schedule_saves(db)==1 and save_queued(setup)==0
    assert db.execute('SELECT COUNT(*) FROM app_deliveries').fetchone()[0]==0
    assert 'PRIVATE' not in db.execute('SELECT error FROM jobs WHERE status=\'failed\'').fetchone()[0]


def test_maintenance_backfills_confirmations_and_waits_when_queue_is_full(setup):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,p=proposed(setup)
    db.execute('DELETE FROM review_saves');db.execute('DELETE FROM jobs');db.commit()
    for n in range(10):
        db.execute("INSERT INTO jobs(update_id,chat_id,user_id,payload,status) VALUES (?,?,?,'{}','queued')",('busy'+str(n),22,11))
    db.commit()
    assert schedule_saves(db)==0
    assert db.execute('SELECT state FROM review_saves').fetchone()[0]=='pending'
    db.execute("UPDATE jobs SET status='completed'");db.commit()
    assert schedule_saves(db)==1 and save_queued(setup)==0


def test_notification_failure_retries_separately_and_blocked_user_recovers_on_return(setup,tmp_path):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup);save_queued(setup)
    manager.add(playlist['id'],R.item_result(db,item),tmp_path)
    class Offline:
        def message(self,*a):raise TelegramError('Не удалось соединиться с Telegram.')
    assert deliver_notices(db,Offline())==0
    assert db.execute('SELECT state FROM review_notices').fetchone()[0]=='pending'
    assert manager.contains(playlist['id'],R.item_result(db,item))
    db.execute('UPDATE review_notices SET retry_at=0');db.commit()
    class Blocked:
        def message(self,*a):raise TelegramError('Telegram не принял sendMessage (код 403).')
    deliver_notices(db,Blocked())
    assert db.execute('SELECT state FROM review_notices').fetchone()[0]=='blocked'
    R.remember_profile(db,{'id':101,'username':'returned_friend'})
    assert deliver_notices(db,bot)==1 and deliver_notices(db,bot)==0


def test_unconfirmed_preview_or_another_playlist_never_notifies(setup,tmp_path):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,p=proposed(setup);save_queued(setup)
    db.execute("UPDATE review_proposals SET status='preview'");db.commit()
    manager.add(playlist['id'],R.item_result(db,item),tmp_path)
    assert not db.execute('SELECT 1 FROM review_notices').fetchone()
    db.execute("UPDATE review_proposals SET status='confirmed'");db.commit()
    archive=suggestions_playlist(settings,db,disk)
    accepted(db,archive['id'],R.item_result(db,item))
    assert not db.execute('SELECT 1 FROM review_notices').fetchone()


def test_failed_index_does_not_notify_until_addition_completes(setup,tmp_path):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup);save_queued(setup)
    upload=disk.upload
    def fail(*a,**kw):raise RuntimeError('Index unavailable')
    disk.upload=fail
    import pytest
    with pytest.raises(RuntimeError):manager.add(playlist['id'],R.item_result(db,item),tmp_path)
    assert not db.execute('SELECT 1 FROM review_notices').fetchone()
    disk.upload=upload
    manager.add(playlist['id'],R.item_result(db,item),tmp_path)
    assert deliver_notices(db,bot)==1


def test_existing_native_delivery_is_not_bumped_by_suggestion_save(setup):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup)
    from aidj.device_inbox import publish
    publish(db,R.item_result(db,item))
    revision_before=db.execute('SELECT revision FROM app_deliveries').fetchone()[0]
    save_queued(setup)
    assert db.execute('SELECT revision FROM app_deliveries').fetchone()[0]==revision_before


def test_background_saves_do_not_block_owner_listening_and_recover_after_worker_crash(setup):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup)
    assert not R.busy(db,11)
    queued=claim_job(db);job_update(db,queued['id'],status='failed')
    db.execute('UPDATE review_saves SET retry_at=0');db.commit()
    assert schedule_saves(db)==1 and save_queued(setup)==0
    assert db.execute('SELECT attempts FROM review_saves').fetchone()[0]==2


def test_guest_cannot_forge_background_save_to_mutate_owner_library(setup):
    settings,db,disk,bot,manager,original,playlist,*_=setup
    item,_=proposed(setup)
    from aidj.store import enqueue
    job_id=enqueue(db,'forged-save',202,202,0,'save',{'client':'review','action':'save','item_id':item})
    row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    assert process_save(settings,db,row,disk)==1
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()
    assert db.execute('SELECT state FROM review_saves').fetchone()[0]=='queued'
    assert manager.list()[0]['tracks']==1
