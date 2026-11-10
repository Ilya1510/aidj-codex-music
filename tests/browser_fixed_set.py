"""A frozen set keeps its published plan in native transport and real web playback."""
import io
import copy
import os
import wave
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from aidj.webapp import byte_range


def main():
    tracks=[{'id':str(i+1)*24,'md5':str(i+1)*32,'title':'Fixed song '+str(i),'artist':'QA','duration':8,'bytes':100,'format':'mp3'} for i in range(3)]
    frozen={'id':'dj1','name':'DJ 1','revision':'qa1','playlistId':1,'tracks':[{'trackId':tracks[i]['id'],'md5':tracks[i]['md5'],'start':1.,'end':6.,'fadeIn':.2,'fadeOut':.3,'transitionSeconds':0.,'transitionKind':'clean-cut','rate':1.} for i in (2,0,1)]}
    catalog={'access':{'role':'admin','userId':1},'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball','trackIds':[t['id'] for t in tracks],'revision':'qa'}],'djSets':[frozen]}
    wav=io.BytesIO()
    with wave.open(wav,'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\0\0'*(8*8000))
    def api(route):
        path=route.request.url.split('/api/',1)[1]
        if path.startswith('audio/'):
            body=wav.getvalue();start,end,partial=byte_range(route.request.headers.get('range'),len(body));headers={'Accept-Ranges':'bytes'}
            if partial:headers['Content-Range']=f'bytes {start}-{end}/{len(body)}'
            route.fulfill(status=206 if partial else 200,body=body[start:end+1],headers=headers,content_type='audio/wav')
        elif path=='catalog':route.fulfill(json=catalog)
        elif path=='android-version':route.fulfill(status=503,json={'error':'QA'})
        else:route.fulfill(json={'ok':True,'revision':'qa','access':catalog['access']})
    with sync_playwright() as p:
        for native in (True,False):
            browser=launch(p);ctx=browser.new_context(viewport={'width':390,'height':844},service_workers='block');page=ctx.new_page();page.set_default_timeout(8000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            if native:ctx.add_init_script(BRIDGE.replace('version:()=>8','version:()=>38'))
            page.route('**/api/**',api)
            try:
                page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18787'),wait_until='networkidle');page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать DJ 1',exact=True).click()
                page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 2"')
                if native:
                    q=page.evaluate('window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue');assert q['fixedSet']['id']=='dj1' and q['djEnabled'] and not q['djRemix']
                    assert [e['trackId'] for e in q['entries']]==[tracks[i]['id'] for i in (2,0,1)]
                    assert all(e['fixed']['transitionSeconds']==0 for e in q['entries'])
                else:
                    page.wait_for_function('()=>{const a=document.querySelector("audio");return !a.paused&&a.currentTime>=1;}')
                    page.locator('#mini-play').click();assert page.evaluate('document.querySelector("audio").paused')
                page.locator('.bottom-nav [data-view="queue"]').click()
                assert page.locator('main .queue-current').inner_text().find('1:')<0
                assert '1 · 0:01–0:06' in page.locator('main').inner_text()
                assert '2 · 0:01–0:06' in page.locator('main').inner_text()
                assert not page.get_by_role('button',name='Шафл',exact=True).count()
                assert not page.get_by_role('button',name='Поставить следующей',exact=True).count()
                if not native:
                    page.reload(wait_until='networkidle');page.locator('.bottom-nav [data-view="queue"]').click();page.wait_for_function("()=>document.querySelector('main').textContent.includes('DJ 1 · фиксированные')")
                    page.locator('#mini-play').click();page.wait_for_function('()=>!document.querySelector("audio").paused')
                    page.evaluate('document.querySelector("audio").currentTime=5.99');page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 0"');page.wait_for_function('()=>document.querySelector("audio").currentTime>=1')
                    page.locator('#mini-play').click();assert page.evaluate('document.querySelector("audio").paused')
                    baseline=copy.deepcopy(catalog)
                    for changed in ('manual-range','source-md5'):
                        catalog.clear();catalog.update(copy.deepcopy(baseline));page.evaluate("window.dispatchEvent(new Event('online'))")
                        page.wait_for_timeout(100)
                        page.locator('.bottom-nav [data-view="home"]').click();page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать DJ 1',exact=True).click()
                        page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 2"&&!document.querySelector("audio").paused')
                        current=next(t for t in catalog['tracks'] if t['title']=='Fixed song 2')
                        if changed=='manual-range':current['rangeStart']=2.
                        else:current['md5']='f'*32
                        page.evaluate("window.dispatchEvent(new Event('online'))")
                        page.wait_for_function('()=>document.querySelector("audio").paused&&document.querySelector("audio").volume===0')
                        assert 'изменились' in page.locator('#toast').inner_text()
                        page.evaluate('''()=>{const a=document.querySelector('audio');a.currentTime=6.5;a.dispatchEvent(new Event('timeupdate'));a.dispatchEvent(new Event('loadedmetadata'));}''')
                        page.wait_for_timeout(100)
                        assert page.locator('#mini-title').inner_text()=='Fixed song 2'
                        assert page.evaluate('document.querySelector("audio").paused&&document.querySelector("audio").volume===0')
                        page.locator('#mini-play').click();page.wait_for_timeout(100)
                        assert page.evaluate('document.querySelector("audio").paused&&document.querySelector("audio").volume===0')
                    # Metadata can be50ms shorter than the actual stored WAV.
                    # A full-file ending waits for real ended, preserving those
                    # final samples and resume-in-tail without rewinding.
                    catalog.clear();catalog.update(copy.deepcopy(baseline))
                    for track in catalog['tracks']:track['duration']=7.95
                    for cue in catalog['djSets'][0]['tracks']:cue.update(end=7.95,fadeOut=0.)
                    page.evaluate("window.dispatchEvent(new Event('online'))")
                    page.wait_for_timeout(100)
                    page.locator('.bottom-nav [data-view="home"]').click();page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать DJ 1',exact=True).click()
                    page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 2"&&!document.querySelector("audio").paused')
                    page.locator('#mini-play').click()
                    page.evaluate('''()=>{const a=document.querySelector('audio');window.__fileEndEvents=[];a.addEventListener('ended',()=>window.__fileEndEvents.push({time:a.currentTime,ended:a.ended}));a.currentTime=7.96;a.playbackRate=.1;a.dispatchEvent(new Event('timeupdate'));}''')
                    page.wait_for_timeout(100)
                    assert page.locator('#mini-title').inner_text()=='Fixed song 2'
                    assert page.evaluate('document.querySelector("audio").paused&&document.querySelector("audio").currentTime>7.95')
                    page.locator('#mini-play').click()
                    page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 2"&&!document.querySelector("audio").paused&&document.querySelector("audio").currentTime>=7.97')
                    assert page.evaluate('document.querySelector("audio").volume')>.69
                    page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Fixed song 0"')
                    ended=page.evaluate('window.__fileEndEvents');assert len(ended)==1 and ended[0]['ended'] and ended[0]['time']>=7.999
                    page.evaluate('document.querySelector("audio").dispatchEvent(new Event("ended"))')
                    page.wait_for_timeout(100)
                    assert page.locator('#mini-title').inner_text()=='Fixed song 0'
                    page.locator('#mini-play').click()
                assert not errors,errors
            except Exception:
                print('FIXED_SET_FAILED',native,errors,page.locator('main').inner_text()[:1400],flush=True);raise
            finally:ctx.close();browser.close()
    print('FIXED_SET_PASSED native_frozen_transport web_real_bounds pause restore explicit_order queue_labels late_range_md5_invalidation')


if __name__=='__main__':main()
