import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as N
import pytest

from aidj import dj_review as R
from aidj.dj import complete
from aidj.backup import snapshot
from tests.test_playlists import env


def seed(db,letter='a',needs=True,guarded=False,position=50):
    md5=letter*32;aid=letter*24
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:'+letter,'Song '+letter,'Artist',180,md5,12,'/Music/'+letter+'.mp3'))
    db.execute("INSERT INTO dj_analysis VALUES (?,?,'complete',NULL,0)",(md5,aid))
    primary={'version':1,'wordGuarded':True,'bpm':120,'entries':[[0,.7],[position,.8],[position+20,.7]],'exits':[[position+10,.5,.7],[position+20,.5,.7]]}
    acoustic={'version':1,'quality':'acoustic','wordGuarded':False,'needsReview':True,'bpm':120,
              'entries':[[position,.8],[position+20,.7]],'exits':[[position,.5,.7],[position+10,.5,.8],[position+20,.5,.7]]}
    payload={'version':3,'cueSchema':4,'cues':[],'remix':[],'anchors':primary,'needsReview':needs}
    if not guarded:payload['acousticAnchors']=acoustic
    complete(db,md5,payload,180)
    return md5,payload


def opened(db,event='one'):
    card,queued=R.next_card(db,event,11,22);assert queued
    return R.mark_open(db,card['id'],11,22,123,'test-audio-id')


def test_owner_only_card_is_durable_one_active_and_event_deduplicated(env):
    _,db,*_=env;seed(db)
    with pytest.raises(R.ReviewError):R.next_card(db,'foreign',12,22)
    card,queued=R.next_card(db,'one',11,22)
    assert queued and card['point_ms']==70000 and card['quality']=='acoustic' and set(card['roles'])=={'entry','exit'}
    again,queued=R.next_card(db,'two',11,22);assert again['id']==card['id'] and not queued
    assert db.execute("SELECT count(*) FROM jobs WHERE json_extract(payload,'$.client')='dj-review'").fetchone()[0]==1
    with pytest.raises(R.ReviewError):R.owned_card(db,card['id'],11,99)
    R.mark_open(db,card['id'],11,22,123)
    R.vote(db,card['id'],11,22,-1)
    duplicate,queued=R.next_card(db,'one',11,22)
    assert duplicate['id']==card['id'] and duplicate['state']=='voted' and not queued


def test_bad_then_good_vote_reapplies_raw_points_and_does_not_fake_word_guards(env):
    _,db,*_=env;md5,payload=seed(db);card=opened(db)
    assert R.vote(db,card['id'],11,22,-1)['vote']==-1
    assert R.rejected_points(db,md5)==[70000]
    current=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()[0])
    assert not current['acousticAnchors']['entries'] or all(abs(row[0]-70)>.02 for row in current['acousticAnchors']['entries'])
    assert all(abs(row[0]-70)>.02 for row in current['anchors']['entries'])
    R.vote(db,card['id'],11,22,1)
    current=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()[0])
    assert any(row[0]==70 for row in current['acousticAnchors']['entries'])
    assert current['acousticAnchors']['wordGuarded'] is False and R.rejected_points(db,md5)==[]
    with pytest.raises(R.ReviewError):R.vote(db,card['id'],12,22,-1)
    with pytest.raises(R.ReviewError):R.vote(db,card['id'],11,22,True)


def test_comment_stays_with_voted_card_until_explicit_next_and_cancel_does_not_close_it(env):
    _,db,*_=env;seed(db);card=opened(db)
    R.vote(db,card['id'],11,22,-1)
    R.prompt_comment(db,card['id'],11,22)
    assert R.pending_comment(db,11,22)['id']==card['id']
    R.comment(db,card['id'],11,22,'  Здесь обрезан конец слова.  ')
    assert R.pending_comment(db,11,22) is None
    assert db.execute('SELECT text FROM dj_boundary_comments').fetchone()[0]=='Здесь обрезан конец слова.'
    R.prompt_comment(db,card['id'],11,22);R.cancel_comment(db,11,22)
    assert R.owned_card(db,card['id'],11,22)['state']=='voted'
    R.prompt_comment(db,card['id'],11,22);R.next_card(db,'next',11,22)
    assert R.pending_comment(db,11,22) is None


def test_same_priority_review_alternates_songs_and_prioritises_acoustic_points(env):
    _,db,*_=env;first,_=seed(db,'a');second,_=seed(db,'b')
    card=opened(db);R.vote(db,card['id'],11,22,1)
    next_card,queued=R.next_card(db,'next',11,22)
    assert queued and next_card['md5']!=card['md5']
    assert next_card['quality']=='acoustic'


def test_skip_and_finish_cancel_queued_jobs_without_creating_a_vote(env):
    _,db,*_=env;seed(db)
    card,_=R.next_card(db,'one',11,22);R.skip(db,card['id'],11,22)
    assert R.owned_card(db,card['id'],11,22)['state']=='skipped'
    assert db.execute('SELECT status FROM jobs').fetchone()[0]=='cancelled'
    card,_=R.next_card(db,'two',11,22);R.finish_session(db,11,22)
    assert R.owned_card(db,card['id'],11,22)['state']=='closed'
    assert db.execute('SELECT count(*) FROM dj_boundary_votes').fetchone()[0]==0
    R.mark_failed(db,card['id'],11,22)
    assert R.owned_card(db,card['id'],11,22)['state']=='closed'


def test_orphan_cards_recover_as_failed_but_live_queued_work_is_kept(env):
    _,db,*_=env;seed(db);card,_=R.next_card(db,'one',11,22)
    R.recover_jobs(db);assert R.owned_card(db,card['id'],11,22)['state']=='preparing'
    db.execute("UPDATE jobs SET status='failed'");db.commit();R.recover_jobs(db)
    assert R.owned_card(db,card['id'],11,22)['state']=='failed'


def test_votes_comments_and_unfiltered_source_are_in_private_snapshot(env,tmp_path):
    settings,db,*_=env;seed(db);card=opened(db);R.vote(db,card['id'],11,22,-1);R.comment(db,card['id'],11,22,'Слишком ранний вход')
    folder=tmp_path/'backup';folder.mkdir();file,counts=snapshot(settings,folder);copy=sqlite3.connect(file)
    assert counts['dj_boundary_sources']==counts['dj_boundary_points']==counts['dj_boundary_votes']==counts['dj_boundary_comments']==counts['dj_boundary_cards']==1
    assert copy.execute('SELECT value FROM dj_boundary_votes').fetchone()[0]==-1
    assert copy.execute('SELECT text FROM dj_boundary_comments').fetchone()[0]=='Слишком ранний вход'
    assert copy.execute('SELECT count(*) FROM dj_batch_jobs').fetchone()[0]==0
    copy.close()


def test_renderer_builds_exact_ten_second_centered_context_without_publishing_music(env,tmp_path,monkeypatch):
    settings,db,*_=env;seed(db);card,_=R.next_card(db,'one',11,22);commands=[]
    downloads=[]
    class Disk:
        def download(self,remote,file,expected_md5=None):
            downloads.append(remote)
            assert expected_md5==card['md5'];file.write_bytes(b'x'*12)
    def run(command,**kwargs):
        commands.append(command);Path(command[-1]).write_bytes(b'audio clip');return N(returncode=0)
    monkeypatch.setattr('aidj.dj_review.subprocess.run',run)
    monkeypatch.setattr('aidj.audio.duration',lambda file:5 if file.name=='dj-from-point.m4a' else 10)
    value=R.render_clip(settings,db,card,tmp_path,Disk(),'ffmpeg')
    command=commands[0]
    assert command[command.index('-ss')+1]=='65.0' and command.index('-t')<command.index('-i')
    assert 'atrim=duration=10' in command[command.index('-af')+1]
    assert value['duration']==10 and value['pointPosition']==5
    assert len(downloads)==1
    assert len(commands)==2 and commands[1][commands[1].index('-ss')+1]=='70.0'
    assert value['fromPoint']['duration']==5 and value['fromPoint']['pointPosition']==0
    assert value['fromPoint']['sourcePosition']==value['sourcePosition']
    assert db.execute('SELECT count(*) FROM web_assets').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM app_deliveries').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM audio_clips').fetchone()[0]==0


def test_replayed_comment_is_recorded_once_and_preserves_a_new_card_prompt(env):
    _,db,*_=env;seed(db);old=opened(db)
    R.prompt_comment(db,old['id'],11,22)
    R.comment(db,old['id'],11,22,'Неудачный стык',event=100)
    assert R.comment_handled(db,100,11,22)
    assert not R.comment_handled(db,101,11,22)
    R.vote(db,old['id'],11,22,1)
    new,_=R.next_card(db,'new',11,22);R.mark_open(db,new['id'],11,22,124);R.prompt_comment(db,new['id'],11,22)
    before_jobs=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    R.comment(db,old['id'],11,22,'Неудачный стык',event=100)
    assert R.pending_comment(db,11,22)['id']==new['id']
    assert db.execute('SELECT count(*) FROM dj_boundary_comments').fetchone()[0]==1
    assert db.execute('SELECT event_id FROM dj_boundary_comments').fetchone()[0]=='100'
    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==before_jobs
    with pytest.raises(R.ReviewError):R.comment_handled(db,100,12,22)


def test_old_comment_schema_migrates_and_snapshot_restores_event_identifiers(env,tmp_path):
    from aidj.store import connect
    from aidj.config import Settings
    from aidj.backup import snapshot
    settings,db,*_=env;seed(db);card=opened(db);R.comment(db,card['id'],11,22,'Старый комментарий')
    db.execute('DROP INDEX dj_boundary_comment_event')
    db.execute('ALTER TABLE dj_boundary_comments RENAME TO old_comments')
    db.execute('CREATE TABLE dj_boundary_comments(id INTEGER PRIMARY KEY,point_id TEXT NOT NULL,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,text TEXT NOT NULL,created REAL NOT NULL)')
    db.execute('INSERT INTO dj_boundary_comments SELECT id,point_id,user_id,chat_id,text,created FROM old_comments')
    db.execute('DROP TABLE old_comments');db.commit()
    # Snapshot copies the legacy columns into the new nullable schema.
    folder=tmp_path/'legacy-backup';folder.mkdir();file,_=snapshot(settings,folder)
    migrated=connect(settings)
    assert 'event_id' in {row[1] for row in migrated.execute('PRAGMA table_info(dj_boundary_comments)')}
    R.comment(migrated,card['id'],11,22,'Новый комментарий',event=200)
    R.comment(migrated,card['id'],11,22,'Новый комментарий',event=200)
    assert migrated.execute('SELECT count(*) FROM dj_boundary_comments').fetchone()[0]==2
    restored=connect(Settings(state=file.parent))
    assert restored.execute('SELECT event_id FROM dj_boundary_comments').fetchone()[0] is None
    R.comment(restored,card['id'],11,22,'После восстановления',event=201)
    assert R.comment_handled(restored,201,11,22)


def test_disputed_point_sampling_starts_interior_and_rotates_across_song_regions(env):
    _,db,*_=env;md5,payload=seed(db)
    positions=[5,9,45,75,100,150]
    payload['acousticAnchors']['entries']=[[point,.7] for point in positions]
    payload['acousticAnchors']['exits']=[[point,.5,.7] for point in positions]
    complete(db,md5,payload,180)
    actual=[]
    for event in range(4):
        card=opened(db,'region-'+str(event));actual.append(card['position']);R.vote(db,card['id'],11,22,1)
    assert actual==[100,45,150,75]
    assert db.execute('SELECT count(*) FROM dj_boundary_votes').fetchone()[0]==4
