"""Explicit150ms relative RMS; no word/rise/source guard changes."""
import copy,hashlib,json
import pytest
from aidj import fixed_set_relative_entry as relative
from aidj.fixed_set_entry_review import fingerprint
from tests.test_fixed_set_relative_entry import fixture,set_rms,checkpoint,write


def previous_tail():
    data=fixture()
    for i in range(285,290):set_rms(data,i,.4,.4)
    return data


def v2(data,**kwargs):
    return relative.check_entry_v2(data[1],data[2],80.,data[0],data[3],data[4],**kwargs)


def test_only_relative_body_shortens_previous_tail_and_preroll_reference_is_unchanged():
    data=previous_tail();before=copy.deepcopy(data)
    old=relative.check_entry(data[1],data[2],80.,data[0],data[3],data[4]);new=v2(data)
    assert old['reason']=='relative-stereo-valley-below3dB'
    assert new['accepted']and new['version']==2
    assert new['parameters']=={**relative.PARAMETERS,'bodyStartSeconds':-.1}
    assert new['stereoBodyStart']==79.9 and new['stereoBodyEnd']==80.05
    assert new['stereoBodyFrames']==[290,305]
    assert new['vocalPreFrameFirst']==985 and new['vocalPreFrameLastExclusive']==998
    assert new['fadeInSeconds']==.05 and new['nextObservedWordConfident']is False
    assert data==before


@pytest.mark.parametrize('word',[[79.99,80.01,False],[79.99,80.01,True]])
def test_short_body_never_relaxes_whole_word_veto(word):
    data=previous_tail();data[3].append(word)
    assert v2(data)['reason']=='known-word-crosses-entry-guard'


def test_public_gate_replays_exact_v2_and_rejects_policy_or_claim_tamper(tmp_path):
    data=previous_tail();item=checkpoint(fixture());item['entryProof']=v2(data)
    item['entryReviewInputHash']=fingerprint(item,data[0],[])
    folder=tmp_path/'work'/'vocal-evidence';folder.parent.mkdir();write(data,folder)
    assert relative.selection_error(item,data[0],evidence_folder=folder,rejected=[])is None
    for mutation in [lambda p:p.update(version=1),lambda p:p['parameters'].update(bodyStartSeconds=-.15),
            lambda p:p.update(stereoBodyStart=79.85),lambda p:p.update(verifiedWordBoundary=True),
            lambda p:p['parameters'].update(preRiseStartSeconds=-.1)]:
        bad=copy.deepcopy(item);mutation(bad['entryProof'])
        assert relative.selection_error(bad,data[0],evidence_folder=folder,rejected=[])
    assert not v2(data,rejected=[80.1])['accepted']


@pytest.mark.parametrize('variant,expected',[
    ('plain','1732068e5883ef7b464790a834a266049fb3439464e7bb125df69cdf1fab60d3'),
    ('no-future','73bcd07b20a7213656ae1c7517ce0f36fd9859830aa3041232a128370abeaedb'),
    ('far-future','93f58e50191d5967a4f154fe3ac9e162ce1b1f19ecb9f04b219e644892431878'),
    ('False-crossing','028d7e5165e829584de2de923d6fd840a0b8066c6c3b85cf37abaf48d5e28b0f'),
    ('previous-music-tail','5f4aecbdaaccb2f8cf20dbd1f7924984a6dfd423c66699389b748b4e43e64597'),
])
def test_default_v1_complete_proofs_remain_byte_identical(variant,expected):
    # Captured by independently executing HEAD's pre-V2 module on these inputs.
    data=previous_tail()if variant=='previous-music-tail'else fixture()
    if variant=='no-future':data[3][:]=[[0.,79.7,True]]
    if variant=='far-future':data[3][1]=[95.,96.,False]
    if variant=='False-crossing':data[3].append([79.99,80.01,False])
    result=relative.check_entry(data[1],data[2],80.,data[0],data[3],data[4])
    assert hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()==expected


def test_only_typed_explicit_v2_exists_no_arbitrary_geometry():
    for version in [True,2.,'2',3,None]:assert relative._policy(version)is None
    data=fixture()
    with pytest.raises(TypeError):v2(data,before_seconds=.01)
