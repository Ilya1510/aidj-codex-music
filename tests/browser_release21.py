"""Playback21: target duration, compactlive snapshots, offline restore, hard-song collection."""
import json
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from tests.browser_release20 import catalog


def main():
    data=catalog();errors=[]
    for track in data['tracks']:
        # A realistic64point graph across the complete543song playlist. Max256x2serialization is covered by the Node test.
        track['duration']=600
        track['djV4']['anchors']={'version':1,'wordGuarded':True,'bpm':128,
            'entries':[[round(i*2.1234,4),.8] for i in range(64)],
            'exits':[[round(i*2.1234+1,4),.6,.8] for i in range(64)]}
    data['tracks'][0]['djV4']['reviewRecommended']=True
    bridge=BRIDGE.replace('version:()=>8','version:()=>21').replace("'fade_'+key","'web_'+key")
    # Native metadata uses private files with an8MiB limit, not browser5MiB localStorage.
    bridge=bridge.replace("readMeta:key=>localStorage.getItem('web_'+key)||'null'", "readMeta:key=>key==='catalog'?JSON.stringify(window.__qaCatalogValue):localStorage.getItem('web_'+key)||'null'")
    bridge=bridge.replace("writeMeta:(key,value)=>localStorage.setItem('web_'+key,value)", "writeMeta:(key,value)=>key==='catalog'?(window.__qaCatalogValue=JSON.parse(value)):localStorage.setItem('web_'+key,value)")
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block')
        context.add_init_script('window.__qaCatalogValue='+json.dumps(data)+';');context.add_init_script(bridge);page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        def api(route):
            if route.request.url.endswith('/android-version'):route.fulfill(json={'versionCode':21,'versionName':'0.14.0','url':'https://example.invalid/android/releases/21/AI-DJ.apk'})
            else:route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa21','queued':0})
        page.route('**/api/**',api)
        try:
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.locator('.playlist-card').filter(has_text='DJ — проверить переходы').wait_for()
            page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).click()
            page.get_by_role('button',name='DJ: ремикс',exact=True).click()
            slider=page.get_by_role('slider',name='Длительность фрагмента ремикса')
            assert slider.input_value()=='12'
            slider.fill('60');slider.dispatch_event('input');slider.dispatch_event('change')
            page.wait_for_function("()=>JSON.parse(localStorage.getItem('web_playlist-playback'))['1'].remixDurationSeconds===60")
            assert page.locator('#remix-duration-value').inner_text()=='1:00'
            page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.remixDurationSeconds===60)')
            result=page.evaluate('''()=>{
              const q=window.__fadeCalls.filter(x=>x.type==='play').at(-1).queue;
              const current=q.entries.find(e=>e.id===q.currentId),saved=localStorage.getItem('web_player');
              return {size:JSON.stringify(q).length,count:q.entries.length,anchors:q.entries.every(e=>(e.remixAnchors?.entries.length===(e.remixAnchors?.flat?128:64))),
                actual:current.dj.end-current.dj.start,savedSize:saved.length,savedHeavy:JSON.parse(saved).entries.some(e=>e.remixAnchors||e.remixPool||e.remixCues)};
            }''')
            assert result['count']==543 and result['anchors'],result
            assert abs(result['actual']-60)<=3,result
            assert result['size']<8*1024**2 and result['savedSize']<256*1024 and not result['savedHeavy'],result
            # Mimic the real reduced nativepoll; frontend mustretain point metadata.
            page.evaluate('''()=>{
              const q=structuredClone(window.__fadeCalls.filter(x=>x.type==='play').at(-1).queue);
              q.entries=q.entries.map(({remixAnchors,remixAcousticAnchors,remixPool,remixCues,...entry})=>entry);
              q.remixMetadataOmitted=true;
              window.__aidjNativeState({player:{queue:q,paused:false,time:1,duration:600},files:[]});
            }''')
            page.get_by_role('button',name='Воспроизведение · DJ: ремикс',exact=True).click()
            slider=page.get_by_role('slider',name='Длительность фрагмента ремикса');slider.fill('30');slider.dispatch_event('change')
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="queue"&&x.queue.remixDurationSeconds===30&&x.queue.entries.every(e=>(e.remixAnchors?.entries.length===(e.remixAnchors?.flat?128:64))))')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            savedState=context.storage_state();context.close();browser.close()
            browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block',storage_state=savedState)
            context.add_init_script('window.__qaCatalogValue='+json.dumps(data)+';');context.add_init_script(bridge)
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)));page.route('**/api/**',api)
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Воспроизведение · DJ: ремикс',exact=True).click()
            assert page.get_by_role('slider',name='Длительность фрагмента ремикса').input_value()=='30'
            assert not page.evaluate('window.__fadeCalls.some(x=>x.type==="download")')
            assert not errors,errors
            print('RELEASE21_UI_PASSED',result,flush=True)
        except Exception:
            print('RELEASE21_UI_FAILURE',errors,page.locator('body').inner_text()[-1800:],flush=True);raise
        finally:context.close();browser.close()


if __name__=='__main__':main()
