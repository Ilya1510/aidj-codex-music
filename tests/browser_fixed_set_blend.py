"""Real two-deck playback: overlap, synchronous pause, resume, and one promotion."""
import io
import os
import wave
from playwright.sync_api import sync_playwright
from tests.browser_playback import launch
from aidj.webapp import byte_range


def main():
    tracks=[{'id':str(i+1)*24,'md5':str(i+1)*32,'title':'Blend '+str(i),'artist':'QA','duration':12.,'bytes':100,'format':'wav'} for i in range(3)]
    frozen={'id':'dj1','name':'DJ 1','revision':'blend1','playlistId':1,'tracks':[{'trackId':t['id'],'md5':t['md5'],'start':1.,'end':9.,'fadeIn':3.,'fadeOut':.3,'transitionSeconds':3. if i<2 else 0.,'transitionKind':'crossfade' if i<2 else 'clean-cut','rate':1.} for i,t in enumerate(tracks)]}
    catalog={'access':{'role':'admin','userId':1},'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball','trackIds':[t['id'] for t in tracks],'revision':'qa'}],'djSets':[frozen]}
    wav=io.BytesIO()
    with wave.open(wav,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\0\0'*96000)
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
        browser=launch(p);ctx=browser.new_context(viewport={'width':390,'height':844},service_workers='block');page=ctx.new_page();page.set_default_timeout(10000)
        ctx.add_init_script("""window.__decks=[];const RealAudio=window.Audio;window.Audio=function(...args){const a=new RealAudio(...args);window.__decks.push(a);return a;};""")
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/api/**',api)
        try:
            page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18787'),wait_until='networkidle')
            page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать DJ 1',exact=True).click()
            page.wait_for_function('()=>!document.querySelector("audio").paused&&document.querySelector("audio").currentTime>=1')
            page.evaluate('document.querySelector("audio").currentTime=5.6')
            page.wait_for_function('()=>window.__decks.some(a=>!a.paused&&a.currentTime>1.15)&&document.querySelector("audio").currentTime<8.5')
            playing=page.evaluate('''()=>{const a=document.querySelector('audio'),b=window.__decks.find(b=>b!==a&&!b.paused);return {left:a.currentTime,right:b.currentTime,leftVolume:a.volume,rightVolume:b.volume};}''')
            assert playing['rightVolume']<.4 and playing['leftVolume']>.3,playing
            paused=page.evaluate('''()=>{document.querySelector('#mini-play').click();return {main:document.querySelector('audio').paused,secondary:window.__decks.filter(a=>a!==document.querySelector('audio')).every(a=>a.paused&&a.volume===0)};}''')
            assert paused=={'main':True,'secondary':True},paused
            times=page.evaluate('window.__decks.map(a=>a.currentTime)');page.wait_for_timeout(250)
            assert page.evaluate('window.__decks.every((a,i)=>Math.abs(a.currentTime-('+str(times)+')[i])<.01)')
            page.locator('#mini-play').click()
            page.wait_for_function('()=>window.__decks.some(a=>a!==document.querySelector("audio")&&!a.paused)')
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Blend 1"')
            current=page.evaluate('''()=>({time:document.querySelector('audio').currentTime,volume:document.querySelector('audio').volume,playing:!document.querySelector('audio').paused})''')
            assert current['time']>3.7 and current['volume']>.6 and current['playing'],current
            page.wait_for_timeout(250);assert page.locator('#mini-title').inner_text()=='Blend 1'
            page.locator('#mini-play').click();assert page.evaluate('document.querySelector("audio").paused&&window.__decks.every(a=>a.paused)')
            # Repeat with a factual stored-file ending: Chromium emits pause
            # before ended. That event must not stop the already mixed deck.
            for cue in frozen['tracks']:cue['end']=12.
            page.evaluate("window.dispatchEvent(new Event('online'))")
            page.wait_for_timeout(150)
            page.locator('.bottom-nav [data-view="home"]').click();page.locator('.playlist-card').click()
            page.get_by_role('button',name='Слушать DJ 1',exact=True).click()
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Blend 0"&&!document.querySelector("audio").paused')
            page.evaluate('document.querySelector("audio").currentTime=8.6')
            page.wait_for_function('()=>window.__decks.some(a=>a!==document.querySelector("audio")&&!a.paused&&a.currentTime>1.15)')
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Blend 1"')
            eof=page.evaluate('''()=>({time:document.querySelector('audio').currentTime,playing:!document.querySelector('audio').paused})''')
            assert eof['time']>3.7 and eof['playing'],eof
            page.locator('#mini-play').click()
            assert page.evaluate('document.querySelector("audio").paused&&window.__decks.every(a=>a.paused)')
            # An explicit pause wins over a decoder-ended event already queued
            # by the browser. It must not fall through to next(true)/autoplay.
            page.locator('#mini-play').click()
            page.wait_for_function('()=>!document.querySelector("audio").paused')
            title=page.locator('#mini-title').inner_text()
            paused_eof=page.evaluate('''()=>{const a=document.querySelector('audio');Object.defineProperty(a,'ended',{configurable:true,value:true});document.querySelector('#mini-play').click();a.dispatchEvent(new Event('ended'));delete a.ended;return a.paused;}''')
            assert paused_eof
            page.wait_for_timeout(250)
            assert page.locator('#mini-title').inner_text()==title
            assert page.evaluate('document.querySelector("audio").paused&&window.__decks.every(a=>a.paused)')
            assert not errors,errors
            print('FIXED_BLEND_PASSED',playing,current,flush=True)
        finally:ctx.close();browser.close()


if __name__=='__main__':main()
