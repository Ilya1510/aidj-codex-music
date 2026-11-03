"""Keep the same focused input while typing, composing and refreshing the library."""
import os
from playwright.sync_api import sync_playwright

def main():
    stage='launch'
    tracks=[{'id':'a'*24,'title':'Слепая','artist':'ANNA ASTI','duration':180,'md5':'1'*32,'bytes':100,'format':'mp3'},
            {'id':'b'*24,'title':'IMAGINATION','artist':'SPYAIR','duration':173,'md5':'2'*32,'bytes':120,'format':'mp3'}]
    data={'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball QA','trackIds':[t['id'] for t in tracks],'revision':'1','diskUrl':'https://example.invalid'}]}
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block');page=context.new_page()
            page.route('**/api/**',lambda route:route.fulfill(json=data if route.request.url.endswith('/catalog') else {'revision':'test','jobs':[]}))
            page.goto('http://127.0.0.1:18782',wait_until='networkidle');page.locator('.playlist-card').click()
            stage='typing';page.evaluate('''()=>{window.__focusCount=0;document.addEventListener('focusin',e=>{if(e.target.id==='search')window.__focusCount++;});window.__typingField=document.querySelector('#search');}''')
            field=page.get_by_label('Поиск музыки',exact=True);field.click();field.press_sequentially('слеп',delay=30)
            assert page.evaluate('window.__typingField===document.querySelector("#search")'),'Search recreated its input while typing'
            assert page.evaluate('window.__focusCount')==1,'Search restarted focus after each character'
            assert field.input_value()=='слеп' and page.locator('main .song-row').count()==1
            stage='refresh';data['tracks'].append({**tracks[0],'id':'c'*24,'title':'Слепая live'});data['playlists'][0]['trackIds'].append('c'*24)
            page.evaluate('window.dispatchEvent(new Event("online"))');page.locator('main .song-row').nth(1).wait_for()
            assert page.evaluate('window.__typingField===document.querySelector("#search")&&document.activeElement===window.__typingField&&window.__focusCount===1')
            stage='attributes';assert field.get_attribute('autocapitalize')=='none'
            assert field.get_attribute('spellcheck')=='false' and field.get_attribute('autocorrect')=='off'
            stage='cursor';field.evaluate('(e)=>e.setSelectionRange(1,1)');page.keyboard.type('x');assert field.input_value()=='сxлеп'
            assert field.evaluate('(e)=>e.selectionStart')==2
            stage='clear';assert page.get_by_role('button',name='Очистить поиск',exact=True).count()==1
            page.get_by_role('button',name='Очистить поиск',exact=True).click();field.press_sequentially('SPYAIR')
            assert field.input_value()=='SPYAIR' and page.locator('main .song-row').count()==1
            stage='composition';page.get_by_role('button',name='Очистить поиск',exact=True).click()
            page.evaluate('''()=>{const e=document.querySelector('#search');window.__typingField=e;e.dispatchEvent(new CompositionEvent('compositionstart',{data:''}));e.value='слеп';e.dispatchEvent(new InputEvent('input',{data:'слеп',inputType:'insertCompositionText',isComposing:true,bubbles:true}));e.dispatchEvent(new CompositionEvent('compositionend',{data:'слеп'}));}''')
            assert page.evaluate('window.__typingField===document.querySelector("#search")&&document.activeElement===window.__typingField')
            assert field.input_value()=='слеп'
            stage='discover';page.locator('.bottom-nav [data-view=discover]').click();field=page.get_by_label('Найти новую песню',exact=True);assert field.get_attribute('autocapitalize')=='none'
            field.click();field.press_sequentially('spyair')
            assert field.input_value()=='spyair'
            page.evaluate('''()=>{window.__discoveryField=document.querySelector('#discover-query');window.__searchRenders=0;new MutationObserver(m=>window.__searchRenders+=m.length).observe(document.querySelector('main'),{childList:true});window.dispatchEvent(new Event('online'));}''')
            page.wait_for_function('()=>window.__searchRenders>0')
            assert page.evaluate('window.__discoveryField===document.activeElement&&window.__discoveryField===document.querySelector("#discover-query")')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            context.close();browser.close()
        print('SEARCH_UI_PASSED stable_input single_focus no_autocaps cursor composition explicit_case filters',flush=True)
    except Exception as error:print('SEARCH_UI_FAILED',stage,type(error).__name__,str(error)[:1000],flush=True);raise SystemExit(1)

if __name__=='__main__':main()
