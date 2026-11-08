"""Native snapshots cannot turn a pending/failed catalog into an empty library."""
import json
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch


def main():
    song={'id':'1'*24,'title':'Existing song','artist':'QA','md5':'a'*32,'duration':180,'format':'mp3','bytes':100}
    data={'tracks':[song],'playlists':[{'id':1,'name':'Volleyball','trackIds':[song['id']],'revision':'qa'}]}
    bridge=BRIDGE.replace('version:()=>8','version:()=>28').replace(
        '(async()=>{', '(async()=>{if(value.type==="api"&&value.path==="/api/catalog")await window.__catalogGate;')
    bridge+=';window.__catalogGate=new Promise(resolve=>window.__releaseCatalog=resolve);'
    with sync_playwright() as p:
        browser=launch(p)
        try:
            for mode in ('cold','error','unauthorized','cached','empty'):
                if mode!='cold':browser=launch(p)
                context=browser.new_context(viewport={'width':390,'height':844},service_workers='block')
                context.add_init_script(bridge)
                if mode=='cached':context.add_init_script('localStorage.setItem("fade_catalog",'+json.dumps(json.dumps(data))+');')
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                response={'status':503 if mode in ('error','cached') else 401 if mode=='unauthorized' else 200}
                def api(route):
                    if route.request.url.endswith('/catalog'):
                        route.fulfill(status=response['status'],json=({'tracks':[],'playlists':[]} if mode=='empty' else data) if response['status']==200 else {'error':'QA unavailable'})
                    else:route.fulfill(json={'ok':True,'revision':'qa'})
                page.route('**/api/**',api);page.goto('http://127.0.0.1:18782',wait_until='networkidle')
                def snapshot():
                    page.evaluate('''()=>{const state=JSON.parse(AndroidMusic.snapshot());state.files=[{md5:'q'+Math.random()}];window.__aidjNativeState(state);}''')
                snapshot()
                if mode=='cached':assert page.locator('main .song-row').count()==1
                else:
                    assert 'Загружаю библиотеку' in page.locator('main').inner_text()
                    assert not page.locator('.playlist-add').count()
                    assert '0 песен' not in page.locator('main').inner_text()
                page.evaluate('window.__releaseCatalog()')
                if mode=='error':
                    page.get_by_role('heading',name='Не удалось открыть музыку').wait_for();snapshot()
                    assert page.get_by_role('button',name='Повторить',exact=True).is_visible()
                    assert not page.locator('.playlist-add').count()
                    response['status']=200;page.get_by_role('button',name='Повторить',exact=True).click()
                    page.locator('main .song-row').first.wait_for()
                elif mode=='unauthorized':
                    page.get_by_role('link',name='Открыть бота',exact=True).wait_for();snapshot()
                    assert page.get_by_role('link',name='Открыть бота',exact=True).is_visible()
                    assert not page.locator('.playlist-add').count()
                elif mode=='empty':page.locator('.playlist-add').wait_for();assert '0 песен' in page.locator('main').inner_text()
                else:page.locator('main .song-row').first.wait_for()
                assert not errors,errors
                context.close();browser.close()
            print('CATALOG_LOADING_PASSED pending snapshots retry auth cached empty')
        finally:browser.close()


if __name__=='__main__':main()
