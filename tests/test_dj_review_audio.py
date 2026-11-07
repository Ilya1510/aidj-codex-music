"""Real FFmpeg edge padding tests. Run with the CPU analysis service paused."""
from array import array
import hashlib
import json
import math
from pathlib import Path
import subprocess

import pytest
from aidj.config import Settings
from aidj.store import connect,meta_set
from aidj.dj_review import render_clip
from aidj.audio import ffmpeg,duration
from tests.test_audio import audio_file


@pytest.mark.parametrize('center,leading_tone',[(2,False),(6,False),(2,True),(8,True)])
def test_eight_second_source_point_is_at_five_seconds_in_ten_second_aac_context(tmp_path,center,leading_tone):
    parts=([(8,True)] if center==8 else [(center,True),(.25,False),(8-center-.25,True)]) if leading_tone else [(center,False),(8-center,True)]
    original=audio_file(tmp_path/'original.wav',parts)
    body=original.read_bytes();checksum=hashlib.md5(body).hexdigest()
    settings=Settings(state=tmp_path/'state');db=connect(settings);meta_set(db,'owner_id',11);meta_set(db,'owner_chat_id',22)
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',('a'*24,'youtube:audio','Audio point','Artist',8,checksum,len(body),'/Music/source.wav'))
    db.execute('INSERT INTO dj_boundary_points VALUES (?,?,?,?,?,?,?,?)',('b'*24,checksum,center*1000,json.dumps(['exit']),'acoustic','unreviewed',0,0))
    db.execute('INSERT INTO dj_boundary_cards(id,point_id,user_id,chat_id,created,updated) VALUES (?,?,?,?,?,?)',('c'*24,'b'*24,11,22,0,0));db.commit()
    downloads=[]
    class Disk:
        def download(self,remote,target,expected_md5=None):
            downloads.append(remote)
            assert expected_md5==checksum;target.write_bytes(body)
    result=render_clip(settings,db,{'id':'c'*24,'user_id':11,'chat_id':22},settings.state/'work',Disk())
    assert duration(Path(result['local_path']))==pytest.approx(10,abs=.04)
    def decoded(file):
        value=subprocess.run([ffmpeg(),'-nostdin','-v','error','-i',str(file),'-ac','1','-ar','16000','-f','f32le','pipe:1'],capture_output=True,timeout=15,check=True).stdout
        return array('f',value)
    samples=decoded(result['local_path'])
    def rms(samples,at):
        window=samples[int(at*16000):int((at+.1)*16000)]
        return math.sqrt(sum(x*x for x in window)/len(window))
    if leading_tone:
        assert rms(samples,4.7)>.05 and rms(samples,5.05)<.005
        if center!=8:assert rms(samples,5.4)>.05
    else:assert rms(samples,4.7)<.005 and rms(samples,5.2)>.05
    if center==6:assert rms(samples,9)<.005
    following=result['fromPoint']
    assert following['pointPosition']==0 and following['sourcePosition']==center
    assert duration(Path(following['local_path']))==pytest.approx(5,abs=.04)
    after=decoded(following['local_path'])
    if leading_tone:
        # A loud tone immediately BEFORE the chosen point must not leak into
        # the start-only preview. The following 250ms pause must stay intact.
        assert rms(after,.05)<.005
        if center!=8:assert rms(after,.4)>.05
    else:assert rms(after,.2)>.05
    if center in (6,8):assert rms(after,3.5)<.005
    assert len(downloads)==1
    assert original.read_bytes()==body
    assert db.execute('SELECT count(*) FROM web_assets').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM app_deliveries').fetchone()[0]==0
