"""Create, play, rename and delete an aggregate without mutating its sources."""
import copy,json,re
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from tests.browser_release20 import catalog


def main():
    data=catalog();data['tracks']=data['tracks'][:3]
    first,second,third=data['tracks'];first.update(rangeStart=20,rangeEnd=100)
    first['rating']={'likes':2,'dislikes':0,'neutral':0,'total':2,'mine':None,'proposals':0}
    alias={**copy.deepcopy(first),'id':'f'*24,'title':'Same audio alias'};data['tracks'].append(alias)
    data['playlists']=[{'id':1,'name':'Volleyball','trackIds':[first['id'],second['id']],'revision':'a'*20},
                       {'id':2,'name':'Предложка','trackIds':[alias['id'],third['id']],'revision':'b'*20}]
    data['combinedSupported']=True
    before=copy.deepcopy(data['playlists']);requests=[];errors=[]
    def api(route):
        path=route.request.url.split('/api/',1)[-1]
        if path=='edit':
            body=json.loads(route.request.post_data);requests.append(body);action=body['action']
            if action=='combine':
                assert body['sourcePlaylistIds']==[1,2]
                data['playlists'].append({'id':900001001,'name':body['name'],'combined':True,'sourcePlaylistIds':[1,2],
                                         'trackIds':[first['id'],second['id'],third['id']],'revision':'c'*20})
            elif action=='rename-combined':data['playlists'][-1].update(name=body['name'],revision='d'*20)
            elif action=='delete-combined':data['playlists']=[p for p in data['playlists'] if p['id']!=body['playlistId']]
            else:raise AssertionError('Unexpected source mutation: '+action)
            route.fulfill(json=data)
        else:route.fulfill(json=data if path=='catalog' else {'ok':True,'revision':'combined-qa'})
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,service_workers='block')
        context.add_init_script(BRIDGE.replace('version:()=>8','version:()=>27'))
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/api/**',api)
        try:
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            page.get_by_role('button',name='Создать плейлист',exact=True).click()
            page.get_by_label('Название плейлиста').fill('Together')
            page.get_by_role('checkbox',name='Источник: Volleyball',exact=True).check()
            assert page.get_by_role('button',name='Создать объединение',exact=True).is_disabled()
            page.get_by_role('checkbox',name='Источник: Предложка',exact=True).check()
            assert '3 песни' in page.locator('#sheet [role="status"]').inner_text()
            page.get_by_role('button',name='Создать объединение',exact=True).click()
            page.locator('main h1').filter(has_text='Together').wait_for()
            assert page.locator('main .song-row strong').all_text_contents()==[first['title'],second['title'],third['title']]
            assert '2' in page.locator('main .song-row').first.locator('.rating-badges').inner_text()
            assert 'Volleyball + Предложка' in page.locator('.combined-sources').inner_text()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('window.__fadeCalls.some(x=>x.type==="play")')
            q=page.evaluate('window.__fadeCalls.filter(x=>x.type==="play").at(-1).queue')
            assert q['playlistId']==900001001 and [e['trackId'] for e in q['entries']]==[first['id'],second['id'],third['id']]
            assert q['tracks'][first['id']]['rangeStart']==20 and q['tracks'][first['id']]['rangeEnd']==100
            page.locator('main .song-row').first.get_by_role('button',name='Действия с песней',exact=True).click()
            assert page.locator('#sheet').get_by_role('button',name=re.compile('Убрать из')).count()==0
            page.get_by_role('button',name='Добавить в плейлисты',exact=True).click()
            assert page.locator('#sheet label.check').count()==2
            assert not page.locator('#sheet label.check').filter(has_text='Together').count()
            page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
            page.get_by_role('button',name='Действия с плейлистом',exact=True).click()
            assert not page.locator('#sheet').get_by_role('button',name='Изменить порядок',exact=True).count()
            assert not page.locator('#sheet').get_by_role('button',name='Открыть на Диске',exact=True).count()
            page.get_by_role('button',name='Переименовать',exact=True).click()
            page.get_by_label('Название плейлиста').fill('Together renamed');page.get_by_role('button',name='Сохранить',exact=True).click()
            page.locator('main h1').filter(has_text='Together renamed').wait_for()
            page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Together renamed').click()
            page.get_by_role('button',name='Действия с плейлистом',exact=True).click()
            page.get_by_role('button',name='Удалить объединение',exact=True).click()
            page.get_by_role('button',name='Удалить объединение',exact=True).click()
            page.wait_for_function('!document.querySelector("#sheet").open')
            assert data['playlists']==before and len(data['tracks'])==4
            assert [r['action'] for r in requests]==['combine','rename-combined','delete-combined']
            assert not page.evaluate('window.__fadeCalls.some(x=>x.type==="download")')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            print('COMBINED_UI_PASSED create dedupe shared_ratings manual_range queue source_isolation rename reload delete no_auto_download',flush=True)
        except Exception:
            print('COMBINED_UI_FAILED',errors,page.locator('body').inner_text()[-1800:],flush=True);raise
        finally:context.close();browser.close()


if __name__=='__main__':main()
