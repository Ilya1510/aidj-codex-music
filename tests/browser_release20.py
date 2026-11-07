"""APK 20 bridge integration: a full playlist retains 32 guarded remix choices.

Every API and native action is mocked. No real music is fetched or changed.
"""
import json
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE, launch


TRACKS=543
POOL=32


def catalog():
    tracks=[]
    for i in range(TRACKS):
        cue={'start':35,'end':125,'bpm':128,'fade':.42,'mix':3.2,'entryVerified':True,'wordGuarded':True}
        clips=[{**cue,'start':k*6,'end':k*6+12,'fade':.16,'energy':round(.4+(k%6)/10,2)} for k in range(POOL)]
        tracks.append({'id':f'{i+1:024x}','title':f'Guarded song {i+1}','artist':'QA','duration':240,
                       'md5':f'{i+1:032x}','bytes':100,'format':'mp3','djStatus':'complete',
                       'djV4':{'version':4,'cues':[cue],'remix':clips}})
    return {'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball','trackIds':[t['id'] for t in tracks],'revision':'qa20'}],
            'djService':{'state':'idle'},'djWork':None}


def main():
    stage='open';data=catalog();errors=[]
    bridge=BRIDGE.replace('version:()=>8','version:()=>20').replace("'fade_'+key", "'web_'+key")
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block')
        context.add_init_script(bridge);page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        def api(route):
            if route.request.url.endswith('/android-version'):route.fulfill(json={'versionCode':20,'versionName':'0.13.1','url':'https://example.invalid/android/releases/20/AI-DJ.apk'})
            else:route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa20','queued':0})
        page.route('**/api/**',api)
        try:
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).click()
            page.get_by_role('button',name='DJ: ремикс',exact=True).click()
            page.get_by_role('status').filter(has_text='Подготовлены DJ-фрагменты: 543 из 543').wait_for()
            page.get_by_role('button',name='Закрыть',exact=True).click()
            stage='full-playlist-remix';page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.djRemix)')
            result=page.evaluate('''()=>{
              const command=window.__fadeCalls.filter(x=>x.type==='play').at(-1),q=command.queue;
              const saved=localStorage.getItem('web_player'),parsed=JSON.parse(saved);
              return {count:q.entries.length,pools:q.entries.map(e=>e.remixPool?.length),
                packed:q.entries.every(e=>!Object.hasOwn(e,'remixCues')),
                first:q.entries[0],requestBytes:new TextEncoder().encode(JSON.stringify(command)).length,
                requestChars:JSON.stringify(command).length,savedChars:saved.length,savedCount:parsed.entries.length,
                savedPoolCount:parsed.entries[0].remixPool.length,download:window.__fadeCalls.some(x=>x.type==='download')};
            }''')
            assert result['count']==TRACKS and result['savedCount']==TRACKS
            assert result['pools']==[POOL]*TRACKS and result['savedPoolCount']==POOL
            assert result['packed']
            first=result['first'];assert first['dj']['fade']==.16 and first['dj']['wordGuarded'] and first['dj']['entryVerified']
            packed=first['remixPool'][0]
            assert len(packed)==9 and packed[:5]==[0,12,128,.16,3.2] and packed[5] is None and packed[7:]==[1,1]
            assert result['requestChars']<1024**2 and result['requestBytes']<2*1024**2, result
            assert result['savedChars']<2*1024**2, result
            assert not result['download']
            stage='regular-short-fade';page.get_by_role('button',name='Воспроизведение · DJ: ремикс',exact=True).click()
            page.get_by_role('button',name='DJ',exact=True).click()
            page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>{const q=window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue;return q.djEnabled&&!q.djRemix&&q.entries[0].dj?.fade===.42;}')
            assert page.evaluate('window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue.entries[0].dj.start')==35
            assert not page.evaluate('window.__fadeCalls.some(x=>x.type==="download")')
            assert not errors,errors
            print('RELEASE20_UI_PASSED tracks=543 pool=32 request_chars='+str(result['requestChars'])+' saved_chars='+str(result['savedChars'])+' guarded_short_fades no_download',flush=True)
        except Exception as error:
            print('RELEASE20_UI_FAILED',stage,type(error).__name__,str(error)[:1500],flush=True)
            raise
        finally:context.close();browser.close()


if __name__=='__main__':main()
