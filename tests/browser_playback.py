"""Validate persistent native settings and fades on actual HTML audio playback."""
import io
import wave
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings

BRIDGE=r'''(()=>{
  const state={player:{queue:{entries:[],currentId:null,uiRevision:0,tracks:{}},paused:true,time:0,duration:6},files:[],downloads:{active:null,pending:[],completed:0,total:0,paused:false,error:null}};
  window.__fadeCalls=[];
  window.AndroidMusic={version:()=>8,open(){},files:()=>"[]",readMeta:key=>localStorage.getItem('fade_'+key)||'null',writeMeta:(key,value)=>localStorage.setItem('fade_'+key,value),snapshot:()=>JSON.stringify(state),request(id,raw){const value=JSON.parse(raw);window.__fadeCalls.push(value);(async()=>{
    let result={ok:true};if(value.type==='api'){const response=await fetch(value.path,value.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(value.body)}:{});result={status:response.status,body:await response.json()};}
    else if(value.type==='queue'||value.type==='play'){state.player.queue=value.queue;if(value.type==='play')state.player.paused=false;}
    else if(value.type==='pause')state.player.paused=true;
    window.__aidjNativeReply(id,result);window.__aidjNativeState?.(structuredClone(state));
  })().catch(()=>window.__aidjNativeReply(id,{error:'QA bridge failed'}));}};
})();'''

def launch(p):return p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])

def main():
    settings=Settings();stage='native'
    song={'id':'1'*24,'title':'First song','artist':'QA','duration':6,'md5':'a'*32,'bytes':100,'format':'mp3'}
    second={**song,'id':'2'*24,'title':'Second song','md5':'b'*32}
    data={'tracks':[song,second],'playlists':[{'id':1,'name':'Playback QA','trackIds':[song['id'],second['id']],'revision':'1','diskUrl':'https://example.invalid'}]}
    def json_api(route):route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa'})
    try:
        with sync_playwright() as p:
            browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block');context.add_init_script(BRIDGE)
            page=context.new_page();page.route('**/api/**',json_api);page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18782'),wait_until='networkidle')
            page.locator('main .song-row').first.locator('.song-main').click();page.locator('#settings').click()
            page.locator('#sheet-content').get_by_role('button',name='Воспроизведение · Обычное',exact=True).click()
            assert page.get_by_role('button',name='Обычное',exact=True).get_attribute('aria-pressed')=='true'
            assert page.get_by_label('Длительность плавного перехода').is_disabled()
            page.get_by_role('button',name='Плавное',exact=True).click();slider=page.get_by_label('Длительность плавного перехода');slider.fill('2.5');slider.dispatch_event('input');slider.dispatch_event('change')
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="queue"&&x.queue.transitionMs===2500)')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not any(value in page.locator('#sheet-content').inner_text().split() for value in ('undefined','true','false'))
            page.screenshot(path=str(settings.state/'browser-qa/playback-settings.png'))
            stage='restore';page.reload(wait_until='networkidle');page.locator('#settings').click();page.locator('#sheet-content').get_by_role('button',name='Воспроизведение · Плавное',exact=True).click()
            assert page.get_by_label('Длительность плавного перехода').input_value()=='2.5'
            page.get_by_role('button',name='Обычное',exact=True).click();page.get_by_role('button',name='Закрыть',exact=True).click();page.locator('main .song-row').first.locator('.song-main').click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.transitionMs===0)')
            context.close();browser.close()
            stage='real-audio';browser=launch(p);context=browser.new_context(viewport={'width':412,'height':915},service_workers='block');page=context.new_page()
            wav=io.BytesIO()
            with wave.open(wav,'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\x10\x10'*96000)
            audio=wav.getvalue()
            def api(route):
                if '/api/audio/' in route.request.url:
                    request_range=route.request.headers.get('range')
                    if request_range:
                        parts=request_range.removeprefix('bytes=').split('-');start=int(parts[0] or 0);end=min(int(parts[1]) if parts[1] else len(audio)-1,len(audio)-1)
                        route.fulfill(status=206,body=audio[start:end+1],content_type='audio/wav',headers={'Content-Range':f'bytes {start}-{end}/{len(audio)}','Accept-Ranges':'bytes'})
                    else:route.fulfill(body=audio,content_type='audio/wav',headers={'Accept-Ranges':'bytes'})
                else:json_api(route)
            page.route('**/api/**',api);page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18782'),wait_until='networkidle');page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>document.querySelector("audio").currentTime>.1&&!document.querySelector("audio").paused');assert page.evaluate('document.querySelector("audio").volume')==1
            page.locator('#mini-play').click();page.locator('#settings').click();page.locator('#sheet-content').get_by_role('button',name='Воспроизведение · Обычное',exact=True).click();page.get_by_role('button',name='Плавное',exact=True).click()
            slider=page.get_by_label('Длительность плавного перехода');slider.fill('1');slider.dispatch_event('input');slider.dispatch_event('change');page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>{const a=document.querySelector("audio");return !a.paused&&a.currentTime>.1&&a.currentTime<.8&&a.volume>0&&a.volume<.9;}')
            stage='full-gain';page.wait_for_function('()=>{const a=document.querySelector("audio");return a.currentTime>1.1&&a.volume>.99;}')
            stage='fade-out';page.wait_for_function('()=>{const a=document.querySelector("audio");return a.currentTime>5.2&&a.volume<.85;}')
            stage='next-fade-in';page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Second song"',timeout=6000)
            assert page.evaluate('document.querySelector("audio").volume')<.9
            context.close();browser.close()
        print('PLAYBACK_UI_PASSED default native_settings duration settings_restore disable real_fade_in_out_next',flush=True)
    except Exception as error:print('PLAYBACK_UI_FAILED',stage,type(error).__name__,str(error)[:1500],flush=True);raise SystemExit(1)

if __name__=='__main__':main()
