"""Drag ranges in the native UI and verify real browser audio stops at its boundary."""
import io
import json
import wave
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings

BRIDGE=r'''(()=>{
  const state={player:{queue:{entries:[],currentId:null,uiRevision:0,tracks:{}},paused:true,time:0,duration:0},files:[],downloads:{active:null,pending:[],completed:0,total:0,paused:false,error:null}};
  const meta={};window.__rangeState=state;window.__rangeCalls=[];
  window.AndroidMusic={version:()=>7,open(){},files:()=>"[]",readMeta:key=>JSON.stringify(meta[key]??null),writeMeta:(key,value)=>meta[key]=JSON.parse(value),snapshot:()=>JSON.stringify(state),request(id,raw){const v=JSON.parse(raw);window.__rangeCalls.push(v);(async()=>{let result={ok:true};if(v.type==='api'){const response=await fetch(v.path,v.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(v.body)}:{});result={status:response.status,body:await response.json()};}else if(v.type==='play'||v.type==='queue'){state.player.queue=v.queue;if(v.type==='play')state.player.paused=false;state.player.duration=10;}else if(v.type==='pause')state.player.paused=true;else if(v.type==='seek')state.player.time=v.time;window.__aidjNativeReply(id,result);window.__aidjNativeState?.(structuredClone(state));})().catch(()=>window.__aidjNativeReply(id,{error:'QA bridge failed'}));}};
})();'''

def launch(p):return p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])

def main():
    settings=Settings();stage='native'
    try:
        with sync_playwright() as p:
            browser=launch(p);context=browser.new_context(viewport={'width':412,'height':915},is_mobile=True,has_touch=True,service_workers='block');context.add_init_script(BRIDGE)
            first={'id':'1'*24,'title':'First recording','artist':'QA','duration':10,'md5':'a'*32,'bytes':100,'format':'mp3'}
            second={**first,'id':'2'*24,'title':'Next recording','md5':'b'*32}
            data={'tracks':[first,second],'playlists':[{'id':1,'name':'Range QA','trackIds':[first['id'],second['id']],'revision':'1','diskUrl':'https://example.invalid'}]}
            saved=[]
            def route_api(route):
                path=route.request.url.split('/api/')[1]
                if path=='range':
                    value=route.request.post_data_json;saved.append(value);first.update(rangeStart=value['start'],rangeEnd=value['end']);body={'ok':True}
                else:body=data if path=='catalog' else {'revision':'test','jobs':[]}
                route.fulfill(json=body)
            page=context.new_page();page.route('**/api/**',route_api);page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.locator('main .song-row').first.locator('.song-main').click();page.locator('#now-playing').click();page.get_by_role('button',name='Выбрать фрагмент',exact=True).click()
            page.get_by_label('Начало, время',exact=True).fill('0:02');page.get_by_label('Начало, время',exact=True).press('Tab')
            page.get_by_label('Конец, время',exact=True).fill('0:06');page.get_by_label('Конец, время',exact=True).press('Tab')
            page.wait_for_function('()=>window.__rangeCalls.some(x=>x.type==="queue"&&Object.values(x.queue.tracks).some(t=>t.rangeStart===2&&t.rangeEnd===6))')
            stage='drag';box=page.locator('.range-window').bounding_box();page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2);page.mouse.down();page.mouse.move(box['x']+box['width']/2+30,box['y']+box['height']/2,steps=8);page.mouse.up()
            start=page.get_by_label('Начало, время',exact=True).input_value();end=page.get_by_label('Конец, время',exact=True).input_value();assert start!='0:02'
            pair=page.evaluate('''async values=>{const m=await import('/segments.mjs');return values.map(m.parseTime);}''',[start,end]);assert abs(pair[1]-pair[0]-4)<.15
            page.get_by_role('button',name='Слушать фрагмент',exact=True).click()
            assert page.evaluate('window.__rangeCalls.filter(x=>x.type==="play").length')>=2
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(settings.state/'browser-qa/range-editor.png'))
            context.close();browser.close()
            stage='real-audio';browser=launch(p);context=browser.new_context(viewport={'width':412,'height':915},service_workers='block');page=context.new_page()
            first.update(duration=4,rangeStart=1,rangeEnd=2);second.update(duration=4)
            wav=io.BytesIO()
            with wave.open(wav,'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\x10\x10'*64000)
            audio=wav.getvalue()
            def browser_api(route):
                path=route.request.url.split('/api/')[1]
                if path.startswith('audio/'):
                    request_range=route.request.headers.get('range')
                    if request_range:
                        span=request_range.removeprefix('bytes=').split('-');start=int(span[0] or 0);end=min(int(span[1]) if span[1] else len(audio)-1,len(audio)-1)
                        route.fulfill(status=206,body=audio[start:end+1],content_type='audio/wav',headers={'Content-Range':f'bytes {start}-{end}/{len(audio)}','Accept-Ranges':'bytes'})
                    else:route.fulfill(body=audio,content_type='audio/wav',headers={'Accept-Ranges':'bytes'})
                else:route.fulfill(json=data if path=='catalog' else {'ok':True,'revision':'test'})
            page.route('**/api/**',browser_api);page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.locator('.playlist-card').click();page.get_by_role('button',name='Слушать',exact=True).click()
            try:page.wait_for_function('()=>document.querySelector("audio").currentTime>=1&&!document.querySelector("audio").paused',timeout=8000)
            except Exception:
                print('AUDIO_QA_STATE',page.evaluate('''()=>{const a=document.querySelector('audio');return {paused:a.paused,time:a.currentTime,duration:a.duration,seeking:a.seeking,buffered:a.buffered.length,source:a.getAttribute('src'),ready:a.readyState,error:a.error?.message,title:document.querySelector('#mini-title').textContent,toast:document.querySelector('#toast').textContent};}'''),flush=True)
                raise
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Next recording"',timeout=6000)
            stage='repeat-segment';page.locator('main .song-row').first.locator('.song-main').click();page.locator('#now-playing').click()
            page.get_by_role('button',name='Повтор',exact=True).click();page.get_by_role('button',name='Повтор',exact=True).click()
            page.wait_for_timeout(2600)
            assert page.locator('#mini-title').inner_text()=='First recording'
            assert page.evaluate('document.querySelector("audio").currentTime>=.98&&document.querySelector("audio").currentTime<2.2')
            context.close();browser.close()
        print('RANGE_UI_PASSED manual_times drag_window native_queue real_audio_boundary_next repeat_segment',flush=True)
    except Exception as error:print('RANGE_UI_FAILED',stage,type(error).__name__,str(error)[:1500],flush=True);raise SystemExit(1)

if __name__=='__main__':main()
