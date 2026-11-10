import math
import sys
from types import SimpleNamespace

import pytest

from aidj.fixed_set_repair import choose, covered, merge, probe_supports, selection_error, word_clear


def test_fresh_model_tuples_and_cached_json_lists_merge_without_losing_vocals():
    words=[[1,5,True],(2,3,False),(7,8,True)]
    assert merge(words)==[[1,5],[7,8]]
    assert not word_clear(words,2,4)
    assert word_clear(words,5.5,6.5)
    assert covered([[0,4],(4,9)],1,8)


def test_probe_elsewhere_cannot_validate_a_newly_selected_exit():
    point={'fadeStart':170,'time':171.2}
    assert not probe_supports({'start':48,'end':76,'confident':53},point)
    assert not probe_supports({'start':160,'end':180,'confident':0},point)
    assert probe_supports({'start':160,'end':180,'confident':12},point)


@pytest.fixture
def numeric_fixture(monkeypatch):
    # choose() only needs these three elementary numeric operations. Keep
    # selection-policy regressions runnable in CI without installing NumPy or
    # running FFT/ASR work; actual extraction owns those separate dependencies.
    def quantile(values, q):
        ordered = sorted(values)
        if not ordered: raise ValueError('empty envelope')
        at = (len(ordered)-1)*q; lo = math.floor(at); hi = math.ceil(at)
        return ordered[lo]+(ordered[hi]-ordered[lo])*(at-lo)
    def arange(start, end, step):
        for i in range(max(0, math.ceil((end-start)/step))):
            yield start+i*step
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(quantile=quantile, arange=arange, max=max))
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72,
           'fadeIn': .1, 'fadeOut': .4, 'rate': 1,
           'transitionKind': 'clean-cut', 'transitionSeconds': 0}
    song = {'lo': 0, 'hi': 100, 'title': 'Quiet release', 'artist': 'Example', 'lufs': None}
    evidence = {'words': [], 'coverage': [[0, 100]]}
    # A genuinely continuous quiet area, surrounded by full-level music.
    rms = [.1]*7000+[.0002]*200+[.1]*2800
    return old, song, evidence, rms


def test_gold_boundary_keeps_the_actual_approved_entry(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['row']['start'] == 10 and result['row']['end'] == 71.5
    assert result['exit']['method'] == 'owner-listened-quiet'
    assert result['row']['end']-result['row']['fadeOut'] >= 70.15


def test_gold_label_cannot_validate_fade_outside_measured_quiet_window(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 70.2, 'fadeOut': .8}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['exit']['method'] != 'owner-listened-quiet'
    assert result['row']['end']-result['row']['fadeOut'] >= result['exit']['regionStart']+.15-1e-4


def test_gold_label_does_not_bypass_current_manual_source_range(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    song = {**song, 'hi': 71}
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['row']['end'] <= 71
    assert result['exit']['method'] != 'owner-listened-quiet'


def test_explicitly_rejected_boundary_cannot_be_restored_by_old_gold_label(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], rejected=[71.5], gold=gold)
    assert result['status'] == 'ready'
    assert abs(result['row']['end']-71.5) >= .35


def test_unobserved_ordinary_quiet_music_is_not_declared_vocal_free(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    rms = [.004 if level == .0002 else level for level in rms]
    result = choose(old, song, {**evidence, 'coverage': []}, rms, owner=[10])
    assert result['status'] == 'needs-review'
    assert 'row' not in result


def test_short_digital_EOF_requires_physical_padding_and_actual_file_end():
    import copy
    point={'time':99.975,'fade':.15,'fadeStart':99.825,'regionStart':99.7,'regionEnd':100,
           'maxRms':.00001,'relativeMax':.0001,'method':'short-digital-EOF',
           'actualEOF':True,'sourceDuration':100,'metadataDuration':100,'unmeasuredTail':0}
    item={'status':'ready','row':{'end':99.975,'fadeOut':.15},'exit':point,
          'observedWords':[[99.9,100.01,False]]}
    assert selection_error(item) is None
    for patch in [{'actualEOF':False},{'maxRms':.01},{'sourceDuration':104},{'unmeasuredTail':.08}]:
        changed=copy.deepcopy(item);changed['exit'].update(patch)
        assert selection_error(changed)


def test_vocal_evidence_must_cover_whole_fade_and_reject_word_energy():
    import copy
    from aidj.vocal_probe import MODEL_SHA256
    check={'accepted':True,'modelSha256':MODEL_SHA256,'normalizedVocalMaxDbfs':-40,
           'normalizedVocalP95Dbfs':-45,'vocalToMixP95Db':-10,'proofStart':70.6,'proofEnd':72.2,
           'sourceOffsetSeconds':68,'sourceEndSeconds':74,'playbackGainDb':-6,'asrClear':True,
           'normalizedMixMaxDbfs':-34,'normalizedMixP95Dbfs':-36}
    point={'time':72,'fade':1.2,'fadeStart':70.8,'regionStart':70.6,'regionEnd':72.2,
           'maxRms':.5,'relativeMax':1,'method':'vocal-isolated-fade','vocalCheck':check}
    item={'status':'ready','row':{'end':72,'fadeOut':1.2},'exit':point,'observedWords':[]}
    assert selection_error(item) is None
    for patch in [{'normalizedVocalMaxDbfs':-20},{'proofEnd':72},{'modelSha256':'0'*64}]:
        changed=copy.deepcopy(item);changed['exit']['vocalCheck'].update(patch)
        assert selection_error(changed)


def test_assembly_preserves_the_reviewed_float_zero_for_an_uncropped_source(tmp_path,monkeypatch):
    import json
    import sqlite3
    from aidj import fixed_set_entry_review as entry
    from aidj import fixed_set_repair as repair
    from aidj import fixed_set_stereo_verify as stereo
    from aidj.fixed_sets import signature

    state=tmp_path/'state';state.mkdir();work=tmp_path/'work';(work/'tracks').mkdir(parents=True)
    owner=state/'research/dj1-curation';owner.mkdir(parents=True)
    (owner/'owner-entries.json').write_text('{}')
    aid='a'*24;md5='1'*32
    row={'trackId':aid,'md5':md5,'start':0.,'end':99.975,'fadeIn':.08,
         'fadeOut':.15,'rate':1,'transitionSeconds':0,'transitionKind':'clean-cut'}
    point={'time':99.975,'fade':.15,'fadeStart':99.825,'regionStart':99.7,'regionEnd':100.,
           'maxRms':.00001,'relativeMax':.0001,'method':'short-digital-EOF',
           'actualEOF':True,'sourceDuration':100.,'metadataDuration':100.,'unmeasuredTail':0.}
    item={'status':'ready','trackId':aid,'md5':md5,'row':row,'exit':point}
    song={'id':aid,'md5':md5,'bytes':1000,'duration':100.,'lo':0.,'hi':100.,
          'manualStart':False,'lufs':-16.,'peak':-2.,
          'loudnessMethod':'ebu-r128-v1','loudnessStatus':'complete'}
    def forbidden(*args):raise AssertionError('source-start review needs no inference')
    item=entry.review_checkpoint(item,song,[],forbidden)
    (work/'tracks'/(aid+'.json')).write_text(json.dumps(item))
    (work/'baseline.json').write_text(json.dumps({'id':'dj1','playlistId':1,
        'revision':signature([row]),'tracks':[row]}))
    db=sqlite3.connect(state/'bot.sqlite')
    db.executescript('''CREATE TABLE web_assets(id TEXT,md5 TEXT,bytes INTEGER,duration REAL);
        CREATE TABLE playback_ranges(track_id TEXT,start REAL,end REAL);
        CREATE TABLE audio_loudness(md5 TEXT,integrated_lufs REAL,true_peak_dbtp REAL,method TEXT,status TEXT);
        CREATE TABLE dj_evidence(md5 TEXT,payload TEXT);''')
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?)',(aid,md5,1000,100.))
    db.execute('INSERT INTO audio_loudness VALUES (?,?,?,?,?)',(md5,-16.,-2.,'ebu-r128-v1','complete'))
    db.execute('INSERT INTO dj_evidence VALUES (?,?)',(md5,'{}'));db.commit();db.close()
    # Stereo waveform validation has its own fixtures. Keep the actual entry
    # fingerprint/gate and final plan assembly active for this regression.
    monkeypatch.setattr(stereo,'stereo_error',lambda *args:None)
    repair.assemble(SimpleNamespace(state=state),work)
    assert json.loads((work/'plan.json').read_text())['tracks']==[row]
