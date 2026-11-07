"""Only a confirmed Telegram 429 is safe to resubmit; no real network calls."""
from types import SimpleNamespace as N

import pytest
import requests

from aidj.telegram import Telegram,TelegramError


SECRET='FAKE_TOKEN_NOT_FOR_DIAGNOSTICS'


def response(body,status=429):
    return N(status_code=status,json=lambda:body)


def rejected(delay=2,code=429):
    return response({'ok':False,'error_code':code,'description':'private response '+SECRET,
                     'parameters':{'retry_after':delay}},code)


def result(tmp_path,cached=False):
    local=tmp_path/'track.mp3';local.write_bytes(b'AUDIO-FRAME-BEGIN\0'*500)
    return {'track':{'title':'Song','artist':'Artist','url':'https://example.invalid/source'},
            'duration':12,'disk_url':'https://example.invalid/disk','local_path':str(local),
            'telegram_file_id':'cached-id' if cached else None}


def test_multipart_429_reopens_complete_file_and_rebuilds_encoder_before_retry(tmp_path,monkeypatch):
    bot=Telegram({'token':SECRET});item=result(tmp_path);raw=open(item['local_path'],'rb').read()
    streams=[];files=[];bodies=[];starts=[];sleeps=[];timeouts=[]
    def post(url,*,data,headers,timeout):
        stream=data;file=stream.fields['audio'][1];streams.append(stream);files.append(file);starts.append(file.tell());timeouts.append(timeout)
        bodies.append(stream.to_string())
        assert headers['Content-Type']==stream.content_type
        return rejected(3) if len(streams)==1 else response({'ok':True,'result':{'message_id':7}},200)
    def sleep(seconds):
        assert files[-1].closed
        sleeps.append(seconds)
    monkeypatch.setattr(bot.session,'post',post);monkeypatch.setattr('aidj.telegram.time.sleep',sleep)
    assert bot.audio(22,item)['message_id']==7
    assert len(streams)==2 and streams[0] is not streams[1] and files[0] is not files[1]
    assert starts==[0,0] and all(f.closed for f in files) and sleeps==[3]
    assert timeouts==[(15,180),(15,180)]
    for body in bodies:
        assert body.count(raw)==1 and b'Song' in body and SECRET.encode() not in body


@pytest.mark.parametrize('cached',[False,True])
def test_audio_retries_at_most_twice_then_preserves_safe_rate_limit_diagnostics(tmp_path,monkeypatch,cached):
    bot=Telegram({'token':SECRET});item=result(tmp_path,cached);calls=[];sleeps=[]
    def post(*args,**kwargs):
        calls.append(kwargs)
        if 'data' in kwargs:kwargs['data'].to_string()
        return rejected(2)
    monkeypatch.setattr(bot.session,'post',post);monkeypatch.setattr('aidj.telegram.time.sleep',sleeps.append)
    with pytest.raises(TelegramError) as caught:bot.audio(22,item)
    assert len(calls)==3 and sleeps==[2,2]
    assert caught.value.code==429 and caught.value.retry_after==2
    assert SECRET not in str(caught.value) and vars(caught.value)=={'code':429,'retry_after':2}


@pytest.mark.parametrize('cached',[False,True])
def test_cached_and_uploaded_audio_do_not_retry_api400_or_ambiguous_timeout(tmp_path,monkeypatch,cached):
    bot=Telegram({'token':SECRET});item=result(tmp_path,cached);calls=[]
    def api400(*args,**kwargs):
        calls.append(True)
        if 'data' in kwargs:kwargs['data'].to_string()
        return rejected(2,400)
    def never_sleep(seconds):raise AssertionError('only a confirmed API429 may wait/retry')
    monkeypatch.setattr(bot.session,'post',api400);monkeypatch.setattr('aidj.telegram.time.sleep',never_sleep)
    with pytest.raises(TelegramError) as caught:bot.audio(22,item)
    assert len(calls)==1 and caught.value.code==400 and caught.value.retry_after is None
    assert SECRET not in str(caught.value)
    calls.clear()
    def timed_out(*args,**kwargs):
        calls.append(True)
        if 'data' in kwargs:kwargs['data'].to_string()
        raise requests.Timeout('delivery unknown '+bot.base)
    monkeypatch.setattr(bot.session,'post',timed_out)
    with pytest.raises(TelegramError) as caught:bot.audio(22,item)
    assert len(calls)==1 and caught.value.code is None and caught.value.retry_after is None
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize('delay',[None,-1,True,1.5,'2',61,999999999999])
def test_retry_delay_must_be_explicit_bounded_integer(tmp_path,monkeypatch,delay):
    bot=Telegram({'token':SECRET});calls=[]
    monkeypatch.setattr(bot.session,'post',lambda *a,**kw:calls.append(True) or rejected(delay))
    monkeypatch.setattr('aidj.telegram.time.sleep',lambda _:pytest.fail('invalid or excessive retry delay'))
    with pytest.raises(TelegramError) as caught:bot.audio(22,result(tmp_path,True))
    assert len(calls)==1 and caught.value.code==429 and SECRET not in str(caught.value)


def test_http429_without_explicit_api_rejection_never_resubmits(tmp_path,monkeypatch):
    bot=Telegram({'token':SECRET});calls=[]
    monkeypatch.setattr(bot.session,'post',lambda *a,**kw:calls.append(True) or response({'ok':False,'parameters':{'retry_after':1}}))
    monkeypatch.setattr('aidj.telegram.time.sleep',lambda _:pytest.fail('an HTTP status alone is not an explicit Telegram API rejection'))
    with pytest.raises(TelegramError) as caught:bot.audio(22,result(tmp_path,True))
    assert len(calls)==1 and caught.value.code==429 and caught.value.retry_after is None


def test_call_exposes_numeric_api_diagnostics_without_auto_retry_or_private_response(monkeypatch):
    bot=Telegram({'token':SECRET});calls=[]
    monkeypatch.setattr(bot.session,'post',lambda *a,**kw:calls.append(True) or rejected(60))
    with pytest.raises(TelegramError) as caught:bot.call('sendMessage',{'chat_id':22})
    assert len(calls)==1 and caught.value.code==429 and caught.value.retry_after==60
    assert SECRET not in str(caught.value) and vars(caught.value)=={'code':429,'retry_after':60}
    monkeypatch.setattr(bot.session,'post',lambda *a,**kw:response([],502))
    with pytest.raises(TelegramError) as malformed:bot.call('sendMessage')
    assert malformed.value.code==502 and malformed.value.retry_after is None


def test_cached_audio_retries_explicit429_without_reuploading_a_file(tmp_path,monkeypatch):
    bot=Telegram({'token':SECRET});calls=[];sleeps=[]
    def post(url,*,json,timeout):
        calls.append((json,timeout))
        return rejected(60) if len(calls)==1 else response({'ok':True,'result':{'message_id':8}},200)
    monkeypatch.setattr(bot.session,'post',post);monkeypatch.setattr('aidj.telegram.time.sleep',sleeps.append)
    assert bot.audio(22,result(tmp_path,True))['message_id']==8
    assert len(calls)==2 and calls[0]==calls[1] and calls[0][0]['audio']=='cached-id'
    assert calls[0][1]==(15,90) and sleeps==[60]
