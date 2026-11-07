"""An API failure must not prohibit native streaming or queuing downloads."""
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch


def main():
    songs=[{'id':str(i)*24,'title':'Network Song '+str(i),'artist':'QA','duration':240,'md5':str(i)*32,'bytes':100,'format':'mp3'} for i in (1,2)]
    catalog={'tracks':songs,'playlists':[{'id':1,'name':'Network QA','trackIds':[s['id'] for s in songs],'revision':'qa'}]}
    failing=False
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':360,'height':800},service_workers='block');context.add_init_script(BRIDGE.replace('version:()=>8','version:()=>17'))
        page=context.new_page()
        def api(route):
            if failing and route.request.url.endswith('/catalog'):route.abort('failed')
            else:route.fulfill(json=catalog if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa'})
        page.route('**/api/**',api);page.goto('http://127.0.0.1:18782',wait_until='networkidle')
        failing=True;page.locator('#settings').click();page.get_by_role('button',name='Обновить библиотеку',exact=True).click()
        page.wait_for_function('()=>document.querySelector("#connection").textContent==="Нет связи с сервером"')
        page.locator('main .song-row').filter(has_text='Network Song 2').locator('.song-main').click()
        page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="play"&&x.queue.entries.find(e=>e.id===x.queue.currentId).trackId==="222222222222222222222222")')
        page.locator('main .song-row').filter(has_text='Network Song 1').get_by_role('button',name='Скачать',exact=True).click()
        page.wait_for_function('()=>window.__fadeCalls.some(x=>x.type==="download")')
        # Recovery uses an actual native request result, not a browser VPN/network event.
        page.evaluate('window.__aidjNativeState({network:{known:true,reachable:true},files:[]})')
        assert page.locator('#connection').inner_text()==''
        assert 'Эта песня ещё не скачана' not in page.locator('#toast').inner_text()
        print('CONNECTION_UI_PASSED api_failure streaming download_queue native_recovery')
        context.close();browser.close()


if __name__=='__main__':main()
