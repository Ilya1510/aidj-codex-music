"""Exercise native search, listening, download, multi-add and playlist removal UI."""
import copy
import json
import os
from playwright.sync_api import sync_playwright
from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import create_session,digest


def main():
    settings=Settings();db=connect(settings);session=create_session(db)
    old={'id':'a'*24,'title':'Existing song','artist':'Library','md5':'1'*32,'bytes':100,'duration':150,'format':'mp3'}
    song={'id':'b'*24,'title':'Wanted recording','artist':'Singer','md5':'2'*32,'bytes':120,'duration':180,'format':'mp3'}
    data={'tracks':[old],'playlists':[{'id':1,'name':'Volleyball QA','trackIds':[old['id']],'revision':'1','diskUrl':'https://example.invalid'},{'id':2,'name':'Road QA','trackIds':[],'revision':'1','diskUrl':'https://example.invalid'}]}
    jobs=[];mutations=[];query='Singer Wanted recording'
    candidate={'index':0,'source':'yandex','title':song['title'],'artist':song['artist'],'duration':180,'version':'','available':True}
    def route_api(route):
        request=route.request;path=request.url.split('/api/')[1]
        if path=='catalog':body=data
        elif path=='discover' and request.method=='POST':
            payload=request.post_data_json
            if payload['action']=='search':
                job={'id':1,'query':payload['query'],'kind':'search','status':'queued','stage':'Ищу песню…','candidates':[],'trackId':None,'youtubeOnly':False}
            else:
                assert payload['jobId']==1 and payload['index']==0
                job={'id':2,'query':query,'kind':'prepare','status':'queued','stage':'Сохраняю на Диск…','candidates':[],'trackId':None,'youtubeOnly':False}
            jobs.insert(0,job);body=copy.deepcopy(job)
        elif path=='discover':
            if jobs:
                job=jobs[0];job['status']='completed';job['stage']='Готово'
                if job['kind']=='search':job['candidates']=[candidate]
                else:
                    job['trackId']=song['id']
                    if song not in data['tracks']:data['tracks'].append(song)
            body={'jobs':jobs}
        elif path.startswith('discover/'):body=next(x for x in jobs if x['id']==int(path.rsplit('/',1)[1]))
        elif path=='edit':
            payload=request.post_data_json;mutations.append(payload)
            playlist=next(p for p in data['playlists'] if p['id']==payload['playlistId'])
            assert payload['revision']==playlist['revision']
            if payload['action']=='add':playlist['trackIds'].append(payload['trackId'])
            elif payload['action']=='remove':playlist['trackIds'].remove(payload['trackId'])
            else:raise AssertionError('Unexpected edit')
            playlist['revision']=str(int(playlist['revision'])+1);body=data
        else:body={'ok':True}
        route.fulfill(json=body)
    script=r'''(()=>{
      const files=[],state={player:{queue:{entries:[],currentId:null,uiRevision:0,tracks:{}},paused:true,time:0,duration:0},files,downloads:{active:null,pending:[],completed:0,total:0,paused:false,error:null}};
      window.__calls=[];window.__state=state;
      const emit=()=>window.__aidjNativeState?.(structuredClone(state));
      window.AndroidMusic={version:()=>4,open:()=>{},readMeta:key=>localStorage.getItem('qa_'+key)||'null',writeMeta:(key,value)=>localStorage.setItem('qa_'+key,value),files:()=>JSON.stringify(files),snapshot:()=>JSON.stringify(state),
        request(id,raw){const value=JSON.parse(raw);window.__calls.push(value);(async()=>{
          let result={ok:true};
          if(value.type==='api'){const response=await fetch(value.path,value.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(value.body)}:{});result={status:response.status,body:await response.json()};}
          else if(value.type==='queue'||value.type==='play'){state.player.queue=value.queue;if(value.type==='play')state.player.paused=false;const entry=value.queue.entries.find(x=>x.id===value.queue.currentId);state.player.duration=value.queue.tracks[entry?.trackId]?.duration||0;}
          else if(value.type==='download'){for(const track of value.tracks)if(!files.some(x=>x.md5===track.md5))files.push(track);}
          else if(value.type==='pause')state.player.paused=true;
          window.__aidjNativeReply(id,result);emit();
        })().catch(()=>window.__aidjNativeReply(id,{error:'QA bridge error'}));}};
    })();'''
    stage='launch'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':360,'height':780},is_mobile=True,has_touch=True,service_workers='block')
            context.add_cookies([{'name':'aidj_session','value':session,'url':'http://127.0.0.1:18782','httpOnly':True}]);context.add_init_script(script)
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/api/**',route_api)
            page.goto('http://127.0.0.1:18782',wait_until='networkidle')
            stage='search';page.locator('.bottom-nav [data-view=discover]').click();page.get_by_label('Найти новую песню',exact=True).fill(query);page.locator('main').get_by_role('button',name='Найти',exact=True).click()
            page.get_by_role('button',name='Послушать',exact=True).wait_for();assert not mutations
            stage='prepare-listen';page.get_by_role('button',name='Послушать',exact=True).click();page.locator('.discovery-ready').wait_for();page.locator('#mini-play[aria-label="Пауза"]').wait_for()
            assert page.locator('#mini-title').inner_text()==song['title'] and not mutations
            assert page.evaluate('document.querySelector("audio").paused && !document.querySelector("audio").src')
            stage='download';page.locator('.discovery-ready').get_by_role('button',name='Скачать',exact=True).click();page.locator('.discovery-ready').get_by_role('button',name='Скачано',exact=True).wait_for()
            assert page.evaluate('window.__calls.filter(x=>x.type==="download").length')==1
            stage='multi-add';page.get_by_role('button',name='Добавить в плейлисты',exact=True).click();page.get_by_label('Volleyball QA',exact=True).check();page.get_by_label('Road QA',exact=True).check();page.get_by_role('button',name='Добавить',exact=True).click()
            page.wait_for_function('()=>!document.querySelector("#sheet").open');assert [x['action'] for x in mutations]==['add','add']
            stage='remove';page.locator('.bottom-nav [data-view=home]').click();page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
            row=page.locator('main .song-row').filter(has_text=song['title']);row.get_by_role('button',name='Действия с песней',exact=True).click();page.get_by_role('button',name='Убрать из этого плейлиста',exact=True).click();page.get_by_role('button',name='Убрать из плейлиста',exact=True).click()
            page.wait_for_function('()=>!document.querySelector("#sheet").open')
            assert song['id'] not in data['playlists'][0]['trackIds'] and song['id'] in data['playlists'][1]['trackIds']
            assert page.evaluate('window.__state.files.length')==1
            stage='layout';assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.locator('.bottom-nav [data-view=discover]').click();page.locator('.discovery-ready').wait_for()
            assert page.locator('.discovery-ready').inner_text().find('Road QA')>=0
            assert not errors,errors
            page.screenshot(path=str(settings.state/'browser-qa/discover.png'),full_page=True)
            browser.close()
        print('DISCOVER_UI_PASSED search listen native_download multi_add remove_keeps_file narrow_layout',flush=True)
    except Exception as exc:
        print('DISCOVER_UI_FAILED',stage,type(exc).__name__,str(exc).replace(session,'[SESSION]')[:1500],flush=True);raise SystemExit(1)
    finally:db.execute('DELETE FROM web_sessions WHERE digest=?',(digest(session),));db.commit()


if __name__=='__main__':main()
