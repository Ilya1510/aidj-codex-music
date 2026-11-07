"""Real UI clicks with deliberately held Disk responses; synthetic API only."""
import json
import os
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright
from aidj.config import Settings
from aidj.webapp import Handler, Server


def main():
    songs=[{'id':c*24,'md5':c*32,'title':'Песня '+c.upper(),'artist':'Исполнитель',
            'duration':180,'bytes':100,'format':'mp3'} for c in 'abc']
    data={'tracks':songs,'playlists':[{'id':1,'name':'Volleyball QA','revision':'r1','trackIds':[s['id'] for s in songs[:2]]}],
          'review':{'voters':[],'votes':{}}}
    held=[];operations=set();requests=[]
    def route_api(route):
        request=route.request;path=request.url.split('/api/')[1]
        if path=='edit':
            body=request.post_data_json;requests.append(body);held.append((route,body));return
        result=data if path=='catalog' else {'revision':data['playlists'][0]['revision']} if path=='library-version' else {'ok':True}
        route.fulfill(status=200,content_type='application/json',body=json.dumps(result))
    def reply(entry,status=200):
        route,body=entry
        if status==200 and body['opId'] not in operations:
            p=data['playlists'][0];assert body['revision']==p['revision']
            if body['action']=='remove':p['trackIds'].remove(body['trackId'])
            else:p['trackIds'].insert(0,body['trackId'])
            p['revision']='r'+str(len(operations)+2);operations.add(body['opId'])
        route.fulfill(status=status,content_type='application/json',body=json.dumps(data if status==200 else {'error':'Файл изменён вручную.'}))
    script=r'''(()=>{
      const meta=k=>'qa-playlist-'+k,state={player:{queue:{entries:[],currentId:null,tracks:{},uiRevision:0},paused:true,time:0,duration:180},files:[SONG],downloads:{pending:[],active:null,paused:false}};
      window.__calls=[];
      window.AndroidMusic={version:()=>14,readMeta:k=>localStorage.getItem(meta(k))||'null',writeMeta:(k,v)=>localStorage.setItem(meta(k),v),files:()=>JSON.stringify(state.files),snapshot:()=>JSON.stringify(state),
        request(id,raw){const v=JSON.parse(raw);window.__calls.push(v);(async()=>{
          let result={ok:true};if(v.type==='api'){const r=await fetch(v.path,v.body?{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1'},body:JSON.stringify(v.body)}:{});result={status:r.status,body:await r.json()};}
          window.__aidjNativeReply(id,result);
        })().catch(()=>window.__aidjNativeReply(id,{error:'Network unavailable'}));}};
    })();'''.replace('SONG',json.dumps(songs[0]))
    with tempfile.TemporaryDirectory(prefix='aidj-playlist-browser-') as temporary:
        server=Server(('127.0.0.1',0),Handler,Settings(state=Path(temporary)/'state'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('AIDJ_CHROMIUM') or None,headless=True,
                    args=['--no-sandbox','--single-process','--no-zygote','--disable-gpu','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True,service_workers='block')
                context.add_init_script(script);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/**',route_api);url=f'http://127.0.0.1:{server.server_port}/';page.goto(url,wait_until='networkidle')
                def open_playlist():page.locator('.playlist-card').filter(has_text='Volleyball QA').click()
                def remove(title):
                    page.locator('main .song-row').filter(has_text=title).get_by_role('button',name='Действия с песней',exact=True).click()
                    page.get_by_role('button',name='Убрать из этого плейлиста',exact=True).click()
                    page.get_by_role('button',name='Убрать из плейлиста',exact=True).click()
                    page.locator('#sheet').wait_for(state='hidden')
                open_playlist();remove('Песня A')
                page.wait_for_function('()=>JSON.parse(localStorage.getItem("qa-playlist-playlist-changes")).length===1')
                assert page.locator('main .song-row strong').all_text_contents()==['Песня B']
                assert data['playlists'][0]['trackIds']==[s['id'] for s in songs[:2]]
                assert len(held)==1 and held[0][1]['action']=='remove'
                assert page.locator('#connection').inner_text()=='Сохраняю…'
                # Queue an add while the removal is still waiting for Disk.
                page.get_by_role('button',name='Плейлисты',exact=True).click()
                page.locator('main .song-row').filter(has_text='Песня C').get_by_role('button',name='Действия с песней',exact=True).click()
                page.get_by_role('button',name='Добавить в плейлисты',exact=True).click()
                page.locator('#sheet label.check input').check();page.get_by_role('button',name='Добавить',exact=True).click()
                page.locator('#sheet').wait_for(state='hidden');open_playlist()
                assert page.locator('main .song-row strong').all_text_contents()==['Песня C','Песня B']
                assert len(held)==1
                reply(held.pop(0))
                page.wait_for_function('()=>window.__calls.filter(c=>c.type==="api"&&c.path==="/api/edit").length===2')
                assert len(held)==1 and held[0][1]['action']=='add'
                assert page.locator('main .song-row strong').all_text_contents()==['Песня C','Песня B']
                old=held.pop(0);op_id=old[1]['opId']
                # Commit the add but lose its response by reopening the UI.
                data['playlists'][0]['trackIds'].insert(0,songs[2]['id']);data['playlists'][0]['revision']='r3';operations.add(op_id)
                page.reload(wait_until='domcontentloaded')
                # Finish the old document's abandoned route so Playwright can
                # clean up its routing task without an asyncio cancellation.
                try:old[0].abort()
                except Exception:pass
                page.locator('.playlist-card').filter(has_text='Volleyball QA').wait_for();open_playlist()
                page.wait_for_function('()=>window.__calls.some(c=>c.type==="api"&&c.path==="/api/edit")')
                assert len(held)==1 and held[0][1]['opId']==op_id
                assert page.locator('main .song-row strong').all_text_contents()==['Песня C','Песня B']
                reply(held.pop(0));page.wait_for_function('()=>JSON.parse(localStorage.getItem("qa-playlist-playlist-changes")).length===0')
                assert len(data['playlists'][0]['trackIds'])==2 and len(operations)==2
                # A permanent error restores the song and reports the rejection.
                remove('Песня B');page.wait_for_function('()=>JSON.parse(localStorage.getItem("qa-playlist-playlist-changes")).length===1')
                assert page.locator('main .song-row strong').all_text_contents()==['Песня C']
                reply(held.pop(0),400)
                page.wait_for_function('()=>JSON.parse(localStorage.getItem("qa-playlist-playlist-changes")).length===0')
                assert page.locator('main .song-row strong').all_text_contents()==['Песня C','Песня B']
                assert 'Не удалось убрать' in page.locator('#toast').inner_text()
                assert not page.evaluate('window.__calls.some(c=>c.type==="download")')
                assert not errors,errors
                browser.close()
            print('PLAYLIST_UI_PASSED immediate_remove immediate_add serial_requests no_resurrection restart_lost_response same_opId rejection_rollback cached_audio_preserved',flush=True)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
