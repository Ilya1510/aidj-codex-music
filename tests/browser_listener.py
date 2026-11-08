"""Listener controls, account-isolated local playlists and real foreground web DJ."""
import io
import wave
import os
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from aidj.webapp import byte_range


def main():
    tracks=[{'id':str(i+1).zfill(24),'md5':str(i+1).zfill(32),'title':f'Listener song {i}','artist':'QA','duration':30,'bytes':100,'format':'mp3','djStatus':'complete',
             'djV4':{'remix':[{'start':s,'end':s+10,'bpm':128,'fade':.3,'mix':3.2,'wordGuarded':True,'entryVerified':True} for s in (2,16)]}} for i in range(4)]
    identity={'userId':22};writes=[]
    def access():return {'role':'listener','userId':identity['userId'],'editLibrary':False,'rateSongs':False,'discoverSongs':False,'personalPlaylists':True}
    def catalog():return {'access':access(),'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball','trackIds':[t['id'] for t in tracks[:2]],'revision':'qa'},{'id':2,'name':'Предложка','trackIds':[t['id'] for t in tracks[2:]],'revision':'qa'}]}
    def api(route):
        path=route.request.url.split('/api/',1)[1]
        if path=='catalog':route.fulfill(json=catalog())
        elif path=='session':route.fulfill(json={'ok':True,'access':access()})
        elif path=='library-version':route.fulfill(json={'revision':'qa'})
        elif path=='android-version':route.fulfill(status=503,json={'error':'QA'})
        else:writes.append(path);route.fulfill(status=403,json={'error':'Listener forbidden mutation'})
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,service_workers='block')
        context.add_init_script(BRIDGE.replace('version:()=>8','version:()=>36'))
        page=context.new_page();page.set_default_timeout(10000);errors=[];page.on('pageerror',lambda error:errors.append(str(error)));page.route('**/api/**',api)
        try:
            page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18782'),wait_until='networkidle')
            assert not page.get_by_role('button',name='Найти новую песню',exact=True).count()
            assert not page.get_by_role('button',name='Оценки и предложения',exact=True).count()
            assert page.locator('.bottom-nav [data-view="discover"]').is_hidden()
            page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Действия с плейлистом',exact=True).click()
            assert not page.get_by_role('button',name='Переименовать',exact=True).count()
            assert not page.get_by_role('button',name='Изменить порядок',exact=True).count()
            page.get_by_role('button',name='Создать личную копию',exact=True).click()
            page.get_by_label('Название плейлиста',exact=True).fill('Мой Volleyball')
            page.get_by_role('button',name='Сохранить',exact=True).click()
            page.get_by_role('heading',name='Мой Volleyball',exact=True).wait_for()
            assert page.locator('main .song-row').count()==2
            page.get_by_role('button',name='Действия с плейлистом',exact=True).click()
            page.get_by_role('button',name='Переименовать',exact=True).click()
            page.get_by_label('Название плейлиста',exact=True).fill('Только мой')
            page.get_by_role('button',name='Сохранить',exact=True).click()
            page.get_by_role('heading',name='Только мой',exact=True).wait_for()
            page.get_by_role('button',name='Слушать',exact=True).click();page.locator('#now-playing').click()
            assert not page.get_by_role('button',name='Нравится',exact=True).count()
            assert not page.get_by_role('button',name='Выбрать фрагмент',exact=True).count()
            page.get_by_role('button',name='Свернуть плеер',exact=True).click()
            page.reload(wait_until='networkidle')
            assert page.locator('.playlist-card').filter(has_text='Только мой').count()==1
            identity['userId']=33;page.reload(wait_until='networkidle')
            assert page.locator('.playlist-card').filter(has_text='Только мой').count()==0
            assert not writes,writes;assert not errors,errors
        except Exception:
            print('LISTENER_UI_FAILED',errors,page.locator('main').inner_text()[:1000],flush=True);raise
        finally:context.close();browser.close()
        # iPhone's web route uses the same read-only session and a browser DJ
        # scheduler. Exercise actual HTML audio at its selected source bounds.
        browser=launch(p);context=browser.new_context(viewport={'width':390,'height':844},service_workers='block')
        page=context.new_page();page.set_default_timeout(10000);errors=[];page.on('pageerror',lambda error:errors.append(str(error)));page.route('**/api/**',api)
        audio=io.BytesIO()
        with wave.open(audio,'wb') as file:file.setnchannels(1);file.setsampwidth(2);file.setframerate(8000);file.writeframes(b'\0\0'*(30*8000))
        def audio_route(route):
            data=audio.getvalue();start,end,partial=byte_range(route.request.headers.get('range'),len(data));h={'Accept-Ranges':'bytes'}
            if partial:h['Content-Range']=f'bytes {start}-{end}/{len(data)}'
            route.fulfill(body=data[start:end+1],status=206 if partial else 200,content_type='audio/wav',headers=h)
        page.route('**/api/audio/**',audio_route)
        try:
            page.goto(os.environ.get('AIDJ_QA_ORIGIN','http://127.0.0.1:18782'),wait_until='networkidle')
            page.evaluate('''async()=>{const storage=await import('/storage.mjs');await storage.putMeta('playback-preferences',{mode:'remix',remixDurationSeconds:10,remixMaxVisits:1,remixLookahead:10});}''')
            page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball').click()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('()=>{const a=document.querySelector("audio");return !a.paused&&a.currentTime>=2;}')
            assert page.locator('#mini-title').inner_text()=='Listener song 0'
            page.evaluate('''()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));}''')
            assert page.evaluate('document.querySelector("audio").paused')
            page.evaluate('''()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));}''')
            page.locator('#mini-play').click();page.wait_for_function('()=>!document.querySelector("audio").paused')
            page.evaluate('document.querySelector("audio").currentTime=11.99')
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Listener song 1"')
            page.wait_for_function('()=>{const a=document.querySelector("audio");return !a.paused&&a.currentTime>=2;}')
            page.evaluate('document.querySelector("audio").currentTime=11.99')
            page.wait_for_function('()=>document.querySelector("#mini-player").hidden')
            assert page.evaluate('document.querySelector("audio").paused')
            assert not writes,writes;assert not errors,errors
            print('LISTENER_PASSED protected_ui private_playlists account_isolation web_remix foreground_bounds cap_one')
        except Exception:
            print('LISTENER_WEB_DJ_FAILED',errors,page.evaluate('''()=>{const a=document.querySelector('audio');return {paused:a.paused,time:a.currentTime,ready:a.readyState,duration:a.duration,error:a.error?.code,toast:document.querySelector('#toast').textContent,hidden:document.hidden,title:document.querySelector('#mini-title').textContent};}'''),flush=True);raise
        finally:context.close();browser.close()


if __name__=='__main__':main()
