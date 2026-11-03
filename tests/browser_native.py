"""Validate the native bridge UI contract without starting an Android emulator."""
import json
from pathlib import Path
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest
from aidj.web_catalog import catalog


def main():
    s=Settings();db=connect(s);token=create_session(db);data=catalog(db)
    output=s.state/'browser-qa';output.mkdir(exist_ok=True)
    script=r'''
      (()=>{
        const catalog=CATALOG;
        const meta={catalog};
        const state={player:{queue:{entries:[],currentId:null,uiRevision:0,tracks:{}},paused:true,time:0,duration:0},
          files:[catalog.tracks[0]],downloads:{active:null,pending:[],completed:1,total:1,paused:false,error:null}};
        window.__nativeTestState=state;window.__nativeCalls=[];
        const emit=()=>window.__aidjNativeState?.(structuredClone(state));
        window.AndroidMusic={readMeta:k=>JSON.stringify(meta[k]??null),writeMeta:(k,v)=>meta[k]=JSON.parse(v),files:()=>JSON.stringify(state.files),snapshot:()=>JSON.stringify(state),version:()=>1,open:url=>window.__lastNativeLink=url,
          request(id,raw){const value=JSON.parse(raw);window.__nativeCalls.push(value);
            (async()=>{
              let result={ok:true};
              if(value.type==='api'){
                if(value.path==='/api/android-version')result={status:200,body:{versionCode:2,versionName:'0.2.1',url:'https://test.trycloudflare.com/android/releases/2/AI-DJ.apk'}};
                else {const response=await fetch(value.path,value.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(value.body)}:{});result={status:response.status,body:await response.json()};}
              } else if(value.type==='queue'||value.type==='play') {state.player.queue=value.queue;if(value.type==='play')state.player.paused=false;const entry=value.queue.entries.find(x=>x.id===value.queue.currentId);state.player.duration=value.queue.tracks[entry?.trackId]?.duration||0;}
              else if(value.type==='pause')state.player.paused=true;
              else if(value.type==='seek')state.player.time=value.time;
              else if(value.type==='download'){state.downloads.active=value.tracks[0];state.downloads.total=2;}
              window.__aidjNativeReply(id,result);emit();
            })().catch(()=>window.__aidjNativeReply(id,{error:'test bridge failed'}));
          }};
      })();'''.replace('CATALOG',json.dumps(data,ensure_ascii=False))
    stage='start'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':412,'height':915},is_mobile=True,has_touch=True)
            context.add_cookies([{'name':'aidj_session','value':token,'url':'http://127.0.0.1:18782','secure':False,'httpOnly':True}])
            context.add_init_script(script);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            stage='native-play';page.locator('.playlist-card').filter(has_text='Volleyball').click();page.get_by_role('button',name='Слушать',exact=True).click()
            page.locator('#mini-play[aria-label="Пауза"]').wait_for()
            assert page.evaluate('document.querySelector("audio").paused && !document.querySelector("audio").src')
            stage='native-skip';second=page.evaluate('''()=>{const s=window.__nativeTestState;const e=s.player.queue.entries[1];s.player.queue.currentId=e.id;s.player.time=1;s.player.duration=s.player.queue.tracks[e.trackId].duration;window.__aidjNativeState(structuredClone(s));return s.player.queue.tracks[e.trackId].title;}''')
            assert page.locator('#mini-title').inner_text()==second
            stage='download';row=page.locator('main .song-row').filter(has_text=second).first;row.get_by_role('button',name='Скачать',exact=True).click()
            page.locator('#downloads-progress:not([hidden])').wait_for()
            page.evaluate('''()=>{const s=window.__nativeTestState;s.files.push(s.downloads.active);s.downloads={active:null,pending:[],completed:2,total:2,paused:false,error:null};window.__aidjNativeState(structuredClone(s));}''')
            row.get_by_role('button',name='Скачано на устройство',exact=True).wait_for()
            stage='settings';page.locator('#settings').click()
            text=page.locator('#sheet-content').inner_text()
            assert not any(value in text.split() for value in ('undefined','null','true','false')), 'Hidden settings rendered as raw values'
            assert page.get_by_role('button',name='Перенести из Mini App',exact=True).count()==1
            assert page.get_by_role('button',name='Перенести скачанное в APK',exact=True).count()==0
            page.screenshot(path=str(output/'settings-native.png'))
            stage='updates';page.get_by_role('button',name='Установить / обновить Android-приложение',exact=True).click()
            page.wait_for_timeout(200)
            assert page.evaluate('window.__lastNativeLink')=='https://test.trycloudflare.com/android/releases/2/AI-DJ.apk'
            assert not errors
            context.close();browser.close()
        print('NATIVE_UI_CONTRACT_PASSED play skip downloads update_link',flush=True)
    except Exception as exc:
        print('NATIVE_UI_CONTRACT_FAILED',stage,type(exc).__name__,str(exc).replace(token,'[SESSION]')[:1500],flush=True)
        raise SystemExit(1)
    finally:
        db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(token),));db.commit()


if __name__=='__main__':main()
