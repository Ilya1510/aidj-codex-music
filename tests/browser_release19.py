"""Native 19 UI contracts for compact playback, updates, remix and bounded work.

Run against the local web server. All API calls and the Android bridge are fake;
no real media is fetched, changed, or downloaded by this check.
"""
import json
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE, launch
from aidj.config import Settings


ORIGIN = 'http://127.0.0.1:18782'
RELEASE = {'versionCode':20, 'versionName':'0.13.1', 'url':'https://example.invalid/android/releases/20/AI-DJ.apk'}


def catalog():
    songs=[]
    for i in range(14):
        ident=f'{i+1:024x}'
        cue={'start':42.5,'end':152.5,'bpm':128,'fade':1.2,'mix':3.2,'entryVerified':True}
        clips=[{**cue,'start':52+j*32,'end':64+j*32} for j in range(3)]
        songs.append({'id':ident,'title':f'Release song {i+1}','artist':'QA','duration':240,'md5':f'{i+1:032x}','bytes':100,'format':'mp3',
                      'djStatus':'pending' if i else 'complete', 'djV3':{'version':3,'cues':[cue], 'remix':clips}})
    songs[2].update(rangeStart=30, rangeEnd=75)
    return {'tracks':songs,'playlists':[{'id':1,'name':'Volleyball','trackIds':[x['id'] for x in songs],'revision':'qa'}], 'djService':{'state':'idle'},'djWork':None}


def bridge():
    value=BRIDGE.replace('version:()=>8','version:()=>19')
    return value.replace('open(){}', 'open(url){window.__openedUrl=url;}')


def context_with(browser,data,release=RELEASE):
    context=browser.new_context(viewport={'width':360,'height':640},is_mobile=True,has_touch=True,service_workers='block')
    context.add_init_script(bridge())
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    def api(route):
        if route.request.url.endswith('/android-version'):
            if release is None:route.abort('connectionfailed')
            else:route.fulfill(json=release)
        else:route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa','queued':10})
    page.route('**/api/**',api)
    page.goto(ORIGIN,wait_until='networkidle')
    page.locator('.playlist-card').filter(has_text='Volleyball').wait_for()
    return context,page,errors


def main():
    stage='updates';data=catalog()
    with sync_playwright() as p:
        browser=launch(p)
        try:
            context,page,errors=context_with(browser,data)
            notice=page.locator('#update-notice');notice.wait_for(state='visible')
            assert '0.13.1' in notice.get_attribute('aria-label')
            positions=page.evaluate('''()=>({gear:document.querySelector('#settings').getBoundingClientRect().toJSON(),notice:document.querySelector('#update-notice').getBoundingClientRect().toJSON()})''')
            assert positions['notice']['y']>=positions['gear']['y']+positions['gear']['height']-1
            notice.click();page.wait_for_function('(url)=>window.__openedUrl===url',arg=RELEASE['url'])
            assert 'Офлайн' not in page.locator('#connection').inner_text()
            stage='settings-window';page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).click()
            assert not page.evaluate('window.__fadeCalls.some(x=>x.path==="/api/dj")'), 'Opening settings must not enqueue the playlist'
            page.get_by_role('button',name='DJ',exact=True).click()
            page.get_by_label('Только переходы',exact=True).check()
            page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.djAudition&&x.queue.djEnabled)')
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.path==="/api/dj")')
            requests=page.evaluate('window.__fadeCalls.filter(x=>x.path==="/api/dj").map(x=>x.body)')
            assert requests and all(len(request.get('trackIds',[]))<=10 and len(request.get('nextTrackIds',[]))<=10 for request in requests)
            stage='compact';page.locator('#mini-title').click()
            assert page.locator('#full-content .cover').count()==0
            transport=page.locator('#full-content .transport').bounding_box()
            assert transport and transport['y']>=0 and transport['y']+transport['height']<=640
            page.locator('#full-player').get_by_role('button',name='Следующая песня',exact=True).wait_for(state='visible')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            output=Settings().state/'browser-qa';output.mkdir(exist_ok=True)
            page.screenshot(path=str(output/'release19-compact-player.png'))
            stage='remix';page.get_by_role('button',name='Свернуть плеер',exact=True).click()
            page.get_by_role('button',name='Воспроизведение · DJ',exact=True).click()
            page.get_by_role('button',name='DJ: ремикс',exact=True).click()
            assert page.get_by_label('Только переходы',exact=True).count()==0
            page.screenshot(path=str(output/'release19-remix-settings.png'))
            page.get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue.djRemix===true')
            queue=page.evaluate('window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue')
            assert queue['djEnabled'] and not queue['djAudition']
            assert len(queue['entries'][0]['remixCues'])==3
            manual=next(entry for entry in queue['entries'] if entry['trackId']==data['tracks'][2]['id'])
            assert not manual.get('remixCues'), 'Manual excerpt must retain its chosen bounds'
            page.get_by_role('button',name='Очередь',exact=True).click()
            assert 'DJ · в очереди' not in page.locator('main').inner_text()
            assert not page.evaluate('window.__fadeCalls.some(x=>x.type==="download")')
            assert not errors,errors
            context.close();browser.close();browser=launch(p)
            stage='current-release'
            context,page,errors=context_with(browser,data,{**RELEASE,'versionCode':19})
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.path==="/api/android-version")')
            assert page.locator('#update-notice').is_hidden()
            assert not errors,errors
            context.close();browser.close();browser=launch(p)
            stage='release-unreachable'
            context,page,errors=context_with(browser,data,None)
            page.wait_for_function('()=>window.__fadeCalls.some(x=>x.path==="/api/android-version")')
            assert page.locator('#update-notice').is_hidden()
            assert not page.locator('#connection').inner_text(), 'Release check must not mark the working library offline'
            assert not errors,errors
            context.close()
            print('RELEASE19_UI_PASSED compact update remix audition bounded_work no_pending_noise',flush=True)
        except Exception as error:
            print('RELEASE19_UI_FAILED',stage,type(error).__name__,str(error)[:1200],flush=True)
            raise
        finally:browser.close()


if __name__=='__main__':main()
