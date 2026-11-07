"""Android 17 UI contract: per-playlist DJ, queue cues, full override, skip, offline."""
import os
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from aidj.config import Settings


def main():
    songs=[{'id':str(i)*24,'title':'DJ Song '+str(i),'artist':'QA','duration':240,'md5':str(i)*32,'bytes':100,'format':'mp3',
            'djStatus':'complete','dj':{'cues':[{'start':20,'end':130,'bpm':126+i,'fade':1.8},{'start':20,'end':100,'bpm':126+i,'fade':1.8}]}} for i in range(1,4)]
    waiting={'id':'4'*24,'title':'Waiting Song','artist':'QA','duration':240,'md5':'4'*32,'bytes':100,'format':'mp3','djStatus':'working'}
    data={'tracks':songs+[waiting],'playlists':[{'id':1,'name':'Volleyball','trackIds':[s['id'] for s in songs]+[waiting['id']],'revision':'one'}, {'id':2,'name':'Предложка','trackIds':[songs[0]['id']],'revision':'two'}],'djWork':{'trackId':waiting['id'],'title':waiting['title'],'artist':'QA'},'djService':{'state':'working'}}
    bridge=BRIDGE.replace('version:()=>8','version:()=>17').replace('files:[],downloads:', 'files:'+__import__('json').dumps(songs)+',downloads:')
    bridge=bridge.replace("else if(value.type==='pause')", "else if(value.type==='next'){const q=state.player.queue,at=q.entries.findIndex(e=>e.id===q.currentId);q.currentId=q.entries[at+1]?.id||q.currentId;}\n    else if(value.type==='pause')")
    stage='open';errors=[]
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block');context.add_init_script(bridge)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        def api(route):route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa','queued':3})
        page.route('**/api/**',api)
        try:
            page.goto('http://127.0.0.1:18782',wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball').click()
            assert page.locator('main .dj-ready').count()==3
            assert page.locator('main .dj-working').count()==1
            assert 'DJ готовит: QA — Waiting Song' in page.locator('main').inner_text()
            page.get_by_role('button',name='Только DJ',exact=True).click();assert page.locator('main .song-row').count()==3
            page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).click();page.get_by_role('button',name='DJ',exact=True).click()
            page.get_by_role('status').filter(has_text='Подготовлены DJ-фрагменты: 3 из 4').wait_for()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.path==="/api/dj")')
            page.get_by_role('button',name='Покороче',exact=True).click();page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.djEnabled&&x.queue.playlistId===1&&x.queue.entries[0].dj.end===100)')
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.path==="/api/dj"&&x.body.nextTrackIds?.length===3)')
            stage='full';page.locator('#mini-title').click();page.get_by_role('button',name='Доиграть полностью',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="queue"&&x.queue.entries[0].djFull)')
            assert 'DJ · песня целиком' in page.locator('#full-content').inner_text()
            page.locator('#full-player').get_by_role('button',name='Следующая песня',exact=True).click()
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="DJ Song 2"')
            assert 'Доиграть полностью' in page.locator('#full-content').inner_text()
            stage='queue';page.get_by_role('button',name='Очередь',exact=True).last.click()
            assert page.get_by_text('DJ · 0:00–1:40',exact=True).count()>=1
            page.get_by_role('button',name='Убрать из очереди',exact=True).last.click()
            assert page.evaluate('window.__fadeCalls.filter(x=>x.type==="queue").at(-1).queue.entries.length')==2
            stage='other-playlist';page.get_by_role('button',name='Музыка',exact=True).click();page.locator('.playlist-card').filter(has_text='Предложка').click()
            assert page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).count()==1
            page.get_by_role('button',name='Слушать',exact=True).click();page.wait_for_function('()=>window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue.djEnabled===false')
            stage='restore';page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball').click();assert page.get_by_role('button',name='Только DJ',exact=True).get_attribute('aria-pressed')=='true';page.get_by_role('button',name='Воспроизведение · DJ',exact=True).click()
            assert page.get_by_role('button',name='Покороче',exact=True).get_attribute('aria-pressed')=='true'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not any(word in page.locator('#sheet-content').inner_text().split() for word in ('null','undefined','true','false'))
            output=Settings().state/'browser-qa';output.mkdir(exist_ok=True);page.screenshot(path=str(output/'dj-settings.png'))
            page.get_by_role('button',name='Закрыть',exact=True).click();page.get_by_role('button',name='Слушать',exact=True).click();page.locator('#mini-title').click();page.screenshot(path=str(output/'dj-player.png'))
            stage='offline';page.get_by_role('button',name='Свернуть плеер',exact=True).click();context.set_offline(True)
            page.wait_for_function('()=>!navigator.onLine');page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue.djEnabled===true')
            assert not any(x['type']=='download' for x in page.evaluate('window.__fadeCalls'))
            assert not errors,errors
            print('DJ_UI_PASSED per_playlist cues length full skip queue restore cached_files')
        except Exception as exc:
            print('DJ_UI_FAILED',stage,type(exc).__name__,str(exc)[:1000]);raise
        finally:context.close();browser.close()


if __name__=='__main__':main()
