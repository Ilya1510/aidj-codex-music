"""Bounded mobile browser QA against the real catalog. No credentials in output."""
import json
from pathlib import Path
import tempfile
import time
import sys

import os
from playwright.sync_api import sync_playwright

from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest
from aidj.web_catalog import catalog


def wait_audio(page):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if page.evaluate('() => !document.querySelector("audio").paused && document.querySelector("audio").currentTime > .1'):
            return
        page.wait_for_timeout(200)
    raise AssertionError('audio did not start')


def main():
    settings=Settings();db=connect(settings);token=create_session(db)
    before=db.execute("SELECT COUNT(*) FROM playlist_tracks WHERE state='complete'").fetchone()[0]
    data=catalog(db)
    track=min((x for x in data['tracks'] if x['duration']>60),key=lambda x:x['bytes'])
    output=settings.state/'browser-qa';output.mkdir(exist_ok=True)
    stage='launch'
    offline_only='--offline-only' in sys.argv
    errors=[]
    try:
        with tempfile.TemporaryDirectory(prefix='music-browser-',dir=settings.state/'work') as profile,sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,
                headless=True,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--single-process','--no-zygote','--disable-extensions'])
            context=browser.new_context(viewport={'width':412,'height':915},device_scale_factor=1,is_mobile=True,has_touch=True)
            context.add_cookies([{'name':'aidj_session','value':token,'url':'http://127.0.0.1:18782','httpOnly':True,'secure':False,'sameSite':'Strict'}])
            page=context.new_page();page.on('pageerror',lambda error:errors.append(type(error).__name__))
            page.on('crash',lambda:print('BROWSER_PAGE_CRASH',flush=True))
            stage='home';page.goto('http://127.0.0.1:18782',wait_until='networkidle',timeout=30000)
            page.get_by_role('heading',name='Моя музыка',exact=True).wait_for()
            assert page.evaluate('document.body.scrollWidth <= innerWidth'), 'mobile overflow'
            page.screenshot(path=str(output/'home.png'),full_page=False)
            if not offline_only:
                stage='playlist';page.locator('.playlist-card').filter(has_text='Volleyball').click()
                page.get_by_role('heading',name='Volleyball',exact=True).wait_for()
                page.screenshot(path=str(output/'playlist.png'),full_page=False)
                stage='queue';page.get_by_role('button',name='Шафл',exact=True).click()
                page.get_by_role('button',name='Очередь',exact=True).click()
                page.get_by_role('heading',name='Очередь',exact=True).wait_for()
                expected=len(data['playlists'][0]['trackIds'])
                page.get_by_role('heading',name=f'Далее · {expected-1}',exact=True).wait_for()
                page.locator('main .song-list .song-row').first.get_by_role('button',name='Убрать из очереди',exact=True).click()
                page.get_by_role('heading',name=f'Далее · {expected-2}',exact=True).wait_for()
                page.screenshot(path=str(output/'queue.png'),full_page=False)
                page.locator('#mini-play').click() if page.evaluate('!document.querySelector("audio").paused') else None
            stage='download';page.get_by_role('button',name='Музыка',exact=True).click()
            page.get_by_role('searchbox').fill(track['artist']+' '+track['title'])
            row=page.locator('main .song-row').filter(has_text=track['title']).first
            row.get_by_role('button',name='Скачать',exact=True).click()
            row.get_by_role('button',name='Скачано на устройство',exact=True).wait_for(timeout=90000)
            page.get_by_role('button',name='Скачано',exact=True).click()
            page.get_by_role('heading',name='На устройстве',exact=True).wait_for()
            assert page.locator('main .song-row').count()==1
            stage='offline';context.set_offline(True)
            stage='offline-click'
            page.locator('main .song-row .song-main').first.click()
            stage='offline-play'
            wait_audio(page)
            assert page.evaluate('document.querySelector("audio").src.startsWith("blob:")')
            stage='offline-full-player'
            page.locator('#now-playing').click();page.get_by_role('dialog').filter(has=page.locator('#full-content')).wait_for()
            page.screenshot(path=str(output/'player.png'),full_page=False)
            page.locator('#full-player').press('Escape')
            page.locator('#mini-play').click()
            stage='offline-reload';page.reload(wait_until='domcontentloaded')
            page.get_by_role('heading',name='Моя музыка',exact=True).wait_for(timeout=20000)
            page.get_by_role('button',name='Скачано',exact=True).click()
            page.locator('main .song-row .song-main').first.click()
            wait_audio(page)
            assert page.evaluate('document.querySelector("audio").src.startsWith("blob:")')
            context.close();browser.close()
        after=db.execute("SELECT COUNT(*) FROM playlist_tracks WHERE state='complete'").fetchone()[0]
        assert before==after and not errors
        result={'mobile_layout':True,'shuffle_queue':not offline_only,'queue_removal_preserves_playlist':not offline_only,
                'download_bytes':track['bytes'],'offline_playback':True,'offline_reload':True,'page_errors':len(errors)}
        (output/'result.json').write_text(json.dumps(result,indent=2))
        print('BROWSER_QA_PASSED',json.dumps(result),flush=True)
    except Exception as exc:
        print('BROWSER_QA_FAILED',stage,type(exc).__name__,flush=True)
        print(str(exc).replace(token,'[SESSION]')[:2200],flush=True)
        try:page.screenshot(path=str(output/'failure.png'),full_page=False)
        except Exception:pass
        raise SystemExit(1)
    finally:
        db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(token),));db.commit()


if __name__=='__main__':main()
