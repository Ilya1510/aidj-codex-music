"""Owner feedback suppresses physical boundaries, never alters vocal evidence."""
import copy
import json

import pytest

from aidj.dj import complete,filter_boundary_points,validate
from tests.test_playlists import env


def payload():
    def cue(start,end):return {'start':start,'end':end,'bpm':120,'fade':.7,'mix':3.2,'entryVerified':True,'wordGuarded':True}
    word={'version':1,'wordGuarded':True,'bpm':120,'entries':[[0,.5],[40,.7],[70,.8]],'exits':[[40,.7,.7],[52,.7,.8],[90,.7,.9]]}
    acoustic={**copy.deepcopy(word),'quality':'acoustic','wordGuarded':False,'needsReview':True}
    return {'cueSchema':4,'cues':[cue(0,40),cue(40,90),cue(0,90)],'remix':[cue(28,40),cue(40,52),cue(70,90)],
            'anchors':word,'acousticAnchors':acoustic,'needsReview':False,'reviewRecommended':False,'fullFallback':False}


def test_rejected_physical_point_is_removed_from_both_roles_graphs_and_all_legacy_routes():
    original=validate(payload(),180);before=copy.deepcopy(original)
    filtered=filter_boundary_points(original,[40000])
    assert original==before
    for field in ('anchors','acousticAnchors'):
        for role in ('entries','exits'):assert all(row[0]!=40 for row in filtered[field][role])
    assert [(cue['start'],cue['end']) for cue in filtered['cues']]==[(0,90)]
    assert [(cue['start'],cue['end']) for cue in filtered['remix']]==[(70,90)]
    assert filtered['reviewRecommended'] and not filtered['fullFallback']
    assert filtered['anchors']['wordGuarded'] is True and filtered['acousticAnchors']['wordGuarded'] is False
    assert filtered['remix'][0]['fade']==before['remix'][-1]['fade']


def test_rejection_tolerance_is_twenty_ms_not_a_broader_missing_phrase():
    value=validate({'cueSchema':4,'anchors':{'version':1,'wordGuarded':True,'bpm':0,
        'entries':[[0,.5],[39.9799,.5],[39.98,.5],[40,.5],[40.02,.5],[40.0201,.5]],'exits':[[80,1,.7]]}},180)
    result=filter_boundary_points(value,[40000])
    assert [row[0] for row in result['anchors']['entries']]==[0,39.9799,40.0201]
    assert filter_boundary_points(value,[]) is value
    assert filter_boundary_points(value,[999999,True,-1]) is value


def test_rejecting_every_usable_boundary_retains_an_explicit_reviewable_original_fallback():
    value=validate(payload(),180)
    result=filter_boundary_points(value,[0,28000,40000,52000,70000,90000])
    assert not result['cues'] and not result['remix']
    assert not result['anchors']['entries'] and not result['acousticAnchors']['exits']
    assert result['needsReview'] and result['reviewRecommended'] and result['fullFallback']
    assert validate(result,180)==result


def seed(env):
    settings,db,*_=env
    # The production schema is owned by dj_review; creating the same source table
    # here also exercises upgrades from a database without prior QA metadata.
    db.execute('CREATE TABLE IF NOT EXISTS dj_boundary_sources(md5 TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL)')
    digest='b'*32;aid='a'*24
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:abcdefghijk','Song','Artist',180,digest,123,'/Музыка/song.mp3'))
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',(digest,aid,'pending',None,0));db.commit()
    return db,digest


def test_bad_to_good_restores_exact_cached_source_and_new_analysis_cannot_resurrect_a_bad_point(env,monkeypatch):
    db,digest=seed(env);bad=[]
    monkeypatch.setattr('aidj.dj.apply_boundary_feedback',lambda db,md5,value:filter_boundary_points(value,bad))
    complete(db,digest,payload(),180)
    raw=json.loads(db.execute('SELECT payload FROM dj_boundary_sources WHERE md5=?',(digest,)).fetchone()[0])
    bad.append(40000);complete(db,digest,raw,180)
    visible=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[0])
    assert all(row[0]!=40 for row in visible['anchors']['entries'])
    stored=json.loads(db.execute('SELECT payload FROM dj_boundary_sources WHERE md5=?',(digest,)).fetchone()[0])
    assert any(row[0]==40 for row in stored['anchors']['entries'])
    # A later worker/batch result contains the old point again. The central finish
    # path must suppress it after merging broad-fade legacy alternatives too.
    complete(db,digest,payload(),180)
    visible=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[0])
    assert all(c['start']!=40 and c['end']!=40 for c in visible['cues']+visible['remix'])
    bad.clear();complete(db,digest,stored,180)
    restored=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[0])
    assert restored['anchors']==raw['anchors'] and restored['acousticAnchors']==raw['acousticAnchors']
    assert restored['anchors']['wordGuarded'] is True and restored['acousticAnchors']['wordGuarded'] is False


def test_finish_source_and_filtered_metadata_share_the_callers_transaction(env,monkeypatch):
    db,digest=seed(env)
    monkeypatch.setattr('aidj.dj.apply_boundary_feedback',lambda db,md5,value:filter_boundary_points(value,[40000]))
    complete(db,digest,payload(),180,commit=False)
    assert db.execute('SELECT count(*) FROM dj_boundary_sources').fetchone()[0]==1
    db.rollback()
    assert db.execute('SELECT count(*) FROM dj_boundary_sources').fetchone()[0]==0
    assert db.execute('SELECT status,payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[:]==('pending',None)
