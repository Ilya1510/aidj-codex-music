"""Check optional settings in a browser and Telegram, without exposing sessions."""
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest


def main():
    settings=Settings();db=connect(settings);session=create_session(db)
    stage='launch'
    try:
        with sync_playwright() as p:
            for mode in ('browser','telegram'):
                stage=mode
                browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':360,'height':780},is_mobile=True,has_touch=True,service_workers='block')
                context.add_cookies([{'name':'aidj_session','value':session,'url':'http://127.0.0.1:18782','httpOnly':True}])
                page=context.new_page()
                script='window.Telegram={WebApp:{initData:"qa-fixture",ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){}}};' if mode=='telegram' else ''
                page.route('**/telegram-web-app.js',lambda route:route.fulfill(body=script,content_type='text/javascript'))
                page.route('**/api/auth',lambda route:route.fulfill(json={'ok':True}))
                page.goto('http://127.0.0.1:18782',wait_until='networkidle');page.get_by_role('heading',name='Моя музыка',exact=True).wait_for()
                page.locator('#settings').click();panel=page.locator('#sheet-content');panel.get_by_role('heading',name='Моя музыка',exact=True).wait_for()
                text=panel.inner_text();assert not any(value in text.split() for value in ('undefined','null','true','false')),mode
                assert panel.get_by_role('button',name='Перенести из Mini App',exact=True).count()==0
                assert panel.get_by_role('button',name='Перенести скачанное в APK',exact=True).count()==1
                assert panel.get_by_role('button',name='Открыть в Chrome',exact=True).count()==(1 if mode=='telegram' else 0)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(settings.state/'browser-qa'/('settings-'+mode+'.png')))
                context.close()
                browser.close()
        print('SETTINGS_WEB_PASSED browser telegram hidden_rows_no_primitive_text narrow_layout',flush=True)
    except Exception as exc:
        print('SETTINGS_WEB_FAILED',stage,type(exc).__name__,str(exc).replace(session,'[SESSION]')[:1200],flush=True);raise SystemExit(1)
    finally:db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(session),));db.commit()


if __name__=='__main__':main()
