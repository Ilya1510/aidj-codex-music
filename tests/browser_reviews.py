"""Mobile native-bridge QA with synthetic votes; no production votes or edits."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings
from aidj.webapp import Handler,Server


def main():
    output=Settings().state/'browser-qa';output.mkdir(exist_ok=True)
    songs=[]
    for id,title,likes,dislikes,neutral in [('a','Первый трек',1,0,0),('b','Второй трек',5,2,1),('c','Без оценок',0,0,0),('d','Предложенная песня',2,1,0)]:
        songs.append({'id':id*24,'md5':id*32,'title':title,'artist':'Исполнитель','bytes':100,'duration':180,'format':'mp3',
            'rating':{'likes':likes,'dislikes':dislikes,'neutral':neutral,'total':likes+dislikes+neutral,'mine':None,'proposals':int(id=='d')}})
    data={'tracks':songs,'playlists':[{'id':1,'name':'Volleyball QA','trackIds':[s['id'] for s in songs[:3]],'revision':'qa-1'},
        {'id':2,'name':'Предложка','trackIds':[songs[3]['id']],'revision':'qa-2'}],
        'review':{'playlistId':1,'playlistName':'Volleyball QA','suggestionsPlaylistId':2,'total':12,'participants':3,'poolTrackIds':[s['id'] for s in songs]}}
    data['review']['voters']=[{'id':'owner','label':'Ты','isMine':True},
        {'id':'masha','label':'Маша','username':'masha_qa','isMine':False},
        {'id':'sasha','label':'Саша','username':'sasha_qa','isMine':False}]
    data['review']['votes']={songs[0]['md5']:{'masha':1},songs[1]['md5']:{'masha':1,'sasha':-1},songs[3]['md5']:{'sasha':0}}
    votes=[];edits=[]
    def detail(song):
        return {'trackId':song['id'],'rating':copy.deepcopy(song['rating']),'people':[
            {'label':'@friend_like','username':'friend_like','vote':1,'proposed':False,'isMine':False},
            *([{'label':'@friend_suggest','username':'friend_suggest','vote':-1,'proposed':True,'isMine':False}] if song['id']==songs[3]['id'] else [])],
            'playlistId':1,'playlistName':'Volleyball QA','inPlaylist':song['id'] in data['playlists'][0]['trackIds'],'similar':[]}
    def route_api(route):
        request=route.request;path=request.url.split('/api/')[1]
        if path=='catalog':body=data
        elif path=='reviews':
            payload=request.post_data_json;song=next(s for s in songs if s['id']==payload['trackId']);action=payload['action']
            if action=='vote':
                votes.append(payload);r=song['rating'];key={1:'likes',-1:'dislikes',0:'neutral'}
                if r['mine'] is not None:r[key[r['mine']]]-=1;r['total']-=1
                r['mine']=payload['value']
                if r['mine'] is not None:r[key[r['mine']]]+=1;r['total']+=1
            elif action=='archive':song['rating']['proposals']=0
            body=detail(song)
        elif path=='edit':
            payload=request.post_data_json;edits.append(payload);p=next(p for p in data['playlists'] if p['id']==payload['playlistId'])
            assert payload['action']=='add';p['trackIds'].append(payload['trackId']);p['revision']='changed';body=data
        elif path=='library-version':body={'revision':'qa-'+str(len(votes))}
        elif path=='android-version':body={'versionCode':12,'versionName':'0.9.0','url':'https://example.invalid/AI-DJ.apk'}
        else:body={'ok':True}
        route.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
    script=r'''(()=>{
      const catalog=CATALOG,metaKey=k=>'qa-meta-'+k;
      if(!localStorage.getItem(metaKey('catalog')))localStorage.setItem(metaKey('catalog'),JSON.stringify(catalog));
      const state={player:{queue:{entries:[],currentId:null,tracks:{},uiRevision:0},paused:true,time:0,duration:180},files:[catalog.tracks[0]],downloads:{active:null,pending:[],completed:0,total:0,paused:false}};
      window.__state=state;window.__calls=[];const emit=()=>window.__aidjNativeState?.(structuredClone(state));
      window.AndroidMusic={version:()=>10,readMeta:k=>localStorage.getItem(metaKey(k))||'null',writeMeta:(k,v)=>localStorage.setItem(metaKey(k),v),files:()=>JSON.stringify(state.files),snapshot:()=>JSON.stringify(state),open:url=>window.__opened=url,
      request(id,raw){const v=JSON.parse(raw);window.__calls.push(v);(async()=>{let result={ok:true};
        if(v.type==='api'){const r=await fetch(v.path,v.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(v.body)}:{});result={status:r.status,body:await r.json()};}
        else if(v.type==='play'||v.type==='queue'){state.player.queue=v.queue;if(v.type==='play')state.player.paused=false;}
        else if(v.type==='pause')state.player.paused=true;
        window.__aidjNativeReply(id,result);emit();})().catch(()=>window.__aidjNativeReply(id,{error:'QA network unavailable'}));}};
    })();'''.replace('CATALOG',json.dumps(data,ensure_ascii=False))
    stage='start'
    with tempfile.TemporaryDirectory(prefix='aidj-review-browser-') as temporary:
        server=Server(('127.0.0.1',0),Handler,Settings(state=Path(temporary)/'state'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block')
                context.add_init_script(script);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/**',route_api);page.goto(f'http://127.0.0.1:{server.server_port}/',wait_until='networkidle')
                page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 3 из 3'
                stage='people-filters';page.get_by_role('button',name='По порядку плейлиста',exact=True).click()
                sheet=page.locator('#sheet')
                page.get_by_role('button',name='Маша: Лайк',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 2 из 3'
                page.get_by_role('button',name='Саша: Дизлайк',exact=True).click()
                assert page.locator('main .song-row strong').all_text_contents()==['Второй трек']
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 1 из 3'
                page.get_by_role('button',name='Саша: Нет оценки',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 2 из 3'
                page.get_by_role('button',name='Любое условие',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 3 из 3'
                search=page.get_by_label('Найти участника');search.fill('маша')
                assert sheet.locator('.voter-filter-card').count()==1
                page.get_by_role('button',name='Маша: Дизлайк',exact=True).click()
                assert search.input_value()=='маша' and sheet.locator('.voter-filter-card').count()==1
                page.get_by_role('button',name='Маша: Дизлайк',exact=True).click();search.fill('никогонет')
                assert sheet.locator('.voter-filter-card').count()==0
                search.fill('');page.get_by_role('button',name='Саша: Нет оценки',exact=True).click()
                page.get_by_role('button',name='Все условия',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 1 из 3'
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert sheet.evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                sheet.evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(output/'person-filters.png'))
                sheet.locator('.voter-filter-card').filter(has_text='Маша').scroll_into_view_if_needed();page.screenshot(path=str(output/'person-filter-choices.png'))
                page.get_by_role('button',name='К песням',exact=True).click()
                page.get_by_role('button',name='Слушать',exact=True).click();page.locator('#mini-play[aria-label="Пауза"]').wait_for()
                assert page.evaluate('window.__state.player.queue.entries.map(e=>e.trackId)')==[songs[1]['id']]
                stage='people-persistence';page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                assert page.locator('main .song-row strong').all_text_contents()==['Второй трек']
                assert page.get_by_role('button',name='Убрать условие: Маша',exact=True).count()==1
                page.get_by_role('button',name='Плейлисты',exact=True).click();page.locator('.playlist-card').filter(has_text='Предложка').click()
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 1 из 1'
                assert page.locator('main .person-filter-chip').count()==0
                page.get_by_role('button',name='Плейлисты',exact=True).click();page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 1 из 3'
                stage='people-offline';context.set_offline(True);page.wait_for_function('()=>!navigator.onLine')
                page.get_by_role('button',name='По порядку плейлиста · фильтры включены',exact=True).click()
                page.get_by_role('button',name='Любое условие',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 2 из 3'
                page.get_by_role('button',name='Саша: Нет оценки',exact=True).click()
                assert sheet.locator('.filter-count').inner_text()=='После фильтров: 3 из 3'
                page.get_by_role('button',name='К песням',exact=True).click()
                page.get_by_role('button',name='Убрать условие: Маша',exact=True).click()
                assert page.locator('main .person-filter-chip').count()==1
                context.set_offline(False);page.wait_for_function('()=>navigator.onLine')
                page.get_by_role('button',name='По порядку плейлиста · фильтры включены',exact=True).click()
                page.get_by_role('button',name='Сбросить фильтры и сортировку',exact=True).click()
                assert page.get_by_role('button',name='Саша: Дизлайк',exact=True).get_attribute('aria-pressed')=='false'
                page.get_by_role('button',name='К песням',exact=True).click()
                stage='sort';page.get_by_role('button',name='По порядку плейлиста',exact=True).click();page.get_by_label('Сортировка песен').select_option('likes')
                page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
                assert page.locator('main .song-row strong').all_text_contents()==['Второй трек','Первый трек','Без оценок']
                stage='net-score';page.get_by_role('button',name='Больше лайков',exact=True).click()
                page.get_by_text('Общие фильтры',exact=True).click()
                minimum=page.get_by_label('Минимум: лайки минус дизлайки');minimum.fill('2');minimum.press('Tab')
                assert page.locator('#sheet .filter-count').inner_text()=='После фильтров: 1 из 3'
                assert page.locator('main .song-row strong').all_text_contents()==['Второй трек']
                minimum.fill('4');minimum.press('Tab')
                assert page.locator('#sheet .filter-count').inner_text()=='После фильтров: 0 из 3'
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 0 из 3'
                minimum.fill('-1');minimum.press('Tab')
                assert page.locator('#sheet .filter-count').inner_text()=='После фильтров: 3 из 3'
                minimum.fill('');minimum.press('Tab')
                page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
                stage='filters';page.get_by_role('button',name='Больше лайков',exact=True).click();page.get_by_text('Общие фильтры',exact=True).click();page.get_by_label('Без дизлайков').check();page.get_by_label('Фильтр по оценкам').select_option('rated')
                page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
                assert page.locator('main .song-row strong').all_text_contents()==['Первый трек']
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 1 из 3'
                page.get_by_role('button',name='Больше лайков · фильтры включены',exact=True).click();page.get_by_label('Фильтр по оценкам').select_option('all');page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
                stage='filtered-play';page.get_by_role('button',name='Слушать',exact=True).click();page.locator('#mini-play[aria-label="Пауза"]').wait_for()
                assert page.evaluate('window.__state.player.queue.entries.map(e=>e.trackId)')==[songs[0]['id'],songs[2]['id']]
                assert data['playlists'][0]['trackIds']==[s['id'] for s in songs[:3]] and not edits
                page.locator('#now-playing').click()
                assert not any(x in page.locator('#full-content').inner_text().split() for x in ['null','undefined','true','false'])
                page.locator('#full-content').get_by_role('button',name='Кто оценил',exact=True).click()
                page.get_by_role('button',name='@friend_like',exact=True).wait_for()
                stage='offline-vote';context.set_offline(True);page.wait_for_function('()=>!navigator.onLine')
                page.locator('#sheet').get_by_role('button',name='Не нравится',exact=True).click();page.locator('#sheet .rating-vote[aria-label="Не нравится"][aria-pressed=true]').wait_for()
                assert not votes
                assert page.evaluate('JSON.parse(localStorage.getItem("qa-meta-review-votes"))')[songs[0]['md5']]['value']==-1
                stage='online-sync';context.set_offline(False);page.wait_for_function('()=>Object.keys(JSON.parse(localStorage.getItem("qa-meta-review-votes"))||{}).length===0')
                assert len(votes)==1 and songs[0]['rating']['mine']==-1
                page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click();page.locator('#full-content').get_by_role('button',name='Свернуть плеер',exact=True).click()
                stage='persistence';page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                assert page.locator('main .song-row strong').all_text_contents()==['Без оценок']
                page.get_by_role('button',name='Плейлисты',exact=True).click();page.get_by_role('button',name='Оценки и предложения',exact=True).click();page.get_by_role('button',name='Предложения',exact=True).click()
                stage='proposal';row=page.locator('main .song-row').filter(has_text='Предложенная песня');row.get_by_role('button',name='Действия с песней',exact=True).click();page.locator('#sheet').get_by_role('button',name='Кто оценил',exact=True).click()
                page.get_by_role('button',name='@friend_suggest',exact=True).wait_for()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(output/'review-people.png'))
                page.get_by_role('button',name='Добавить в Volleyball QA',exact=True).click();page.get_by_role('button',name='Снять с разметки',exact=True).wait_for()
                assert songs[3]['id'] in data['playlists'][0]['trackIds'] and len(edits)==1
                page.get_by_role('button',name='Снять с разметки',exact=True).click();page.locator('#sheet').get_by_role('button',name='Закрыть',exact=True).click()
                assert page.locator('main .song-row').count()==0
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 0 из 0'
                assert not page.evaluate('window.__calls.some(x=>x.type==="download")')
                stage='review-player';page.get_by_role('button',name='Размечать',exact=True).click()
                page.locator('#full-content').get_by_role('button',name='Закончить разметку',exact=True).wait_for()
                assert not any(x in page.locator('#full-content').inner_text().split() for x in ['null','undefined','true','false'])
                page.locator('#full-content').get_by_role('button',name='Закончить разметку',exact=True).click()
                assert not any(x in page.locator('#full-content').inner_text().split() for x in ['null','undefined','true','false'])
                stage='pagination-count'
                for n in range(80):
                    extra={**songs[2],'id':f'{n+1000:024x}','md5':f'{n+1000:032x}','title':'Проверка '+str(n)}
                    songs.append(extra);data['playlists'][0]['trackIds'].append(extra['id'])
                page.reload(wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                assert page.locator('main .song-row').count()==70
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 81 из 84'
                page.get_by_label('Поиск музыки').fill('Проверка')
                assert page.locator('main .filter-count').inner_text()=='После фильтров: 80 из 84'
                assert not errors,errors
                browser.close()
            print('REVIEW_UI_PASSED people_and_or multi_vote people_search people_offline people_persistence playlist_isolation sort net_score live_counts pagination_counts full_player_no_null filters filtered_play names offline_vote sync persistence proposal_accept archive no_auto_download',flush=True)
        except Exception as exc:
            print('REVIEW_UI_FAILED',stage,type(exc).__name__,str(exc)[:1600],flush=True);raise SystemExit(1)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
