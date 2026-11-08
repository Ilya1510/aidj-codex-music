"""Real mobile touch gestures, explicit successor, scoped remix and settings."""
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch


def main():
    tracks=[]
    for i in range(84):
        tracks.append({'id':f'{i+1:024x}','md5':f'{i+1:032x}','title':'Haikyuu manual' if i==0 else f'Song {i}',
            'artist':'QA','duration':173.244,'bytes':100,'format':'mp3','djStatus':'complete',
            'djV4':{'anchors':{'version':1,'wordGuarded':True,'bpm':120,'entries':[[s,.5] for s in range(0,161,20)],'exits':[[s+12,.3,.5] for s in range(0,161,20)]}}})
    tracks[0].update(rangeStart=0,rangeEnd=114.832)
    tracks[0]['loudness']={'integratedLufs':-26.12,'truePeakDbtp':-12.08,'method':'ebu-r128-v1'}
    data={'tracks':tracks,'playlists':[{'id':1,'name':'Volleyball','trackIds':[t['id'] for t in tracks],'revision':'queue-qa'}]}
    bridge=BRIDGE.replace('version:()=>8','version:()=>33').replace('window.__fadeCalls=[];','''window.__fadeCalls=[];window.__queueState=state;
    window.__qaForecast=()=>{const q=state.player.queue,at=q.entries.findIndex(e=>e.id===q.currentId);
      state.player.remixPreview={currentId:q.currentId,uiRevision:q.uiRevision,playbackEpoch:q.playbackEpoch||0,segments:q.entries.slice(at+1,at+11).map((e,i)=>({position:i+1,entryId:e.id,trackId:e.trackId,start:e.dj?.start??0,end:e.dj?.end??q.tracks[e.trackId].duration}))};};''')
    bridge=bridge.replace("else if(value.type==='queue'||value.type==='play'){state.player.queue=value.queue;if(value.type==='play')state.player.paused=false;}",'''else if(value.type==='queue'||value.type==='play'){
      if(window.__advanceBeforeQueue){window.__advanceBeforeQueue=false;const q=state.player.queue;q.entries.shift();q.currentId=q.entries[0].id;q.playbackEpoch=(q.playbackEpoch||0)+1;state.player.time=q.entries[0].dj.start+2;}
      const old=state.player.queue,accepted=value.replaceQueue||value.queue.playbackEpoch===(old.playbackEpoch||0);
      if(accepted)state.player.queue={...value.queue,playbackEpoch:(old.playbackEpoch||0)+(old.currentId!==value.queue.currentId?1:0)};
      if(value.type==='play')state.player.paused=false;
      result={ok:true,queueAccepted:accepted,requestedUiRevision:value.queue.uiRevision,player:structuredClone(state.player)};
    }else if(value.type==='metadata'){
      const q=state.player.queue,patch=value.metadata;
      for(const id of Object.keys(q.tracks||{}))if(patch.tracks[id])q.tracks[id]=patch.tracks[id];
      for(const e of q.entries){const p=patch.entries.find(p=>p.trackId===e.trackId);if(p)for(const key of ['remixAnchors','remixAcousticAnchors','remixPool','remixCues']){delete e[key];if(p[key]!=null)e[key]=p[key];}}
      result={ok:true,player:structuredClone(state.player)};
    }''')
    bridge=bridge.replace('window.__aidjNativeReply(id,result);', 'if(value.type==="queue"||value.type==="play")window.__qaForecast();window.__aidjNativeReply(id,result);')
    bridge+=';localStorage.setItem("fade_playback-preferences",JSON.stringify({mode:"remix",remixDurationSeconds:12,remixMaxVisits:3}));'
    with sync_playwright() as p:
        browser=launch(p);context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,service_workers='block')
        context.add_init_script(bridge);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/**',lambda r:r.fulfill(json=data if r.request.url.endswith('/catalog') else {'ok':True,'revision':'qa'}))
        try:
            page.goto('http://127.0.0.1:18782',wait_until='networkidle');page.locator('.playlist-card').filter(has_text='Volleyball').click()
            options=page.get_by_role('button',name='Воспроизведение · DJ: ремикс',exact=True);options.click()
            duration=page.get_by_label('Длительность фрагмента ремикса');visits=page.get_by_label('Фрагментов из одной песни')
            duration.fill('60');duration.dispatch_event('input');duration.dispatch_event('change')
            visits.fill('1');visits.dispatch_event('input');visits.dispatch_event('change')
            page.wait_for_function('JSON.parse(AndroidMusic.readMeta("playlist-playback"))[1].remixMaxVisits===1')
            assert page.evaluate('JSON.parse(AndroidMusic.readMeta("playlist-playback"))[1].remixDurationSeconds')==60
            visits.fill('5');visits.dispatch_event('change');depth=page.get_by_label('Глубина выбора из очереди');depth.fill('50');depth.dispatch_event('change');duration.fill('10');duration.dispatch_event('change')
            page.wait_for_function('JSON.parse(AndroidMusic.readMeta("playlist-playback"))[1].remixDurationSeconds===10')
            assert page.evaluate('JSON.parse(AndroidMusic.readMeta("playlist-playback"))[1].remixMaxVisits')==5
            page.get_by_role('button',name='Закрыть',exact=True).click();options.click()
            assert page.get_by_label('Фрагментов из одной песни').input_value()=='5'
            assert page.get_by_label('Длительность фрагмента ремикса').input_value()=='10'
            assert page.get_by_label('Глубина выбора из очереди').input_value()=='50'
            page.get_by_role('button',name='Закрыть',exact=True).click()
            assert 'входы:' in page.locator('main .song-row').first.inner_text()
            page.get_by_role('button',name='Слушать',exact=True).click()
            page.wait_for_function('window.__fadeCalls.some(c=>c.type==="play")')
            q=page.evaluate('window.__fadeCalls.filter(c=>c.type==="play").at(-1).queue')
            assert q['remixMaxVisits']==5 and q['remixDurationSeconds']==10 and q['remixLookahead']==50
            assert q['entries'][0]['remix'] and q['entries'][0]['dj']['end']<=114.832
            assert q['tracks'][tracks[0]['id']]['loudness']==tracks[0]['loudness']
            current=q['currentId']
            page.locator('#now-playing').click();assert page.locator('#full-shuffle').count()==0
            page.get_by_role('button',name='Свернуть плеер',exact=True).click();page.locator('.bottom-nav [data-view="queue"]').click()
            # Two occurrences of the same audio must remain independently movable.
            page.evaluate('''()=>{const s=window.__queueState;s.player.queue.entries[2].trackId=s.player.queue.entries[1].trackId;s.player.queue.uiRevision=Date.now()+100000;window.__qaForecast();window.__aidjNativeState(structuredClone(s));}''')
            rows=page.locator('.queue-sortable [data-queue-entry]')
            assert rows.locator('.rating-badges').count()==0
            assert all(__import__('re').fullmatch(r'(?:\d+ · \d+:\d+–\d+:\d+|—)',text) for text in rows.locator('.queue-step').all_text_contents())
            assert not page.get_by_role('button',name='Выше в очереди',exact=True).count()
            assert not page.get_by_role('button',name='Ниже в очереди',exact=True).count()
            # Rows retain priority order, while labels display the supplied
            # execution forecast, including a second visit to one entry.
            page.evaluate('''()=>{const s=window.__queueState,q=s.player.queue,e=q.entries.slice(1);
              const step=(position,entry,start)=>({position,entryId:entry.id,trackId:entry.trackId,start,end:start+10});
              s.player.remixPreview={currentId:q.currentId,uiRevision:q.uiRevision,playbackEpoch:q.playbackEpoch,segments:[step(1,e[0],20),step(2,e[2],70),step(3,e[1],40),step(4,e[2],100),step(5,q.entries[0],60)]};
              window.__aidjNativeState(structuredClone(s));}''')
            assert rows.nth(0).locator('.queue-step').all_text_contents()==['1 · 0:20–0:30']
            assert rows.nth(1).locator('.queue-step').all_text_contents()==['3 · 0:40–0:50']
            assert rows.nth(2).locator('.queue-step').all_text_contents()==['2 · 1:10–1:20','4 · 1:40–1:50']
            # An older asynchronous queue snapshot must not erase the accepted plan.
            page.evaluate('''()=>{const stale=structuredClone(window.__queueState);stale.player.queue.uiRevision--;delete stale.player.remixPreview;window.__aidjNativeState(stale);}''')
            assert rows.nth(2).locator('.queue-step').all_text_contents()==['2 · 1:10–1:20','4 · 1:40–1:50']
            page.evaluate('window.__queueState.player.remixPreview.uiRevision--;window.__aidjNativeState(structuredClone(window.__queueState))')
            assert all(text=='—' for text in rows.locator('.queue-step').all_text_contents())
            page.evaluate('window.__qaForecast();window.__aidjNativeState(structuredClone(window.__queueState))')
            original=rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)')
            rows.nth(2).get_by_role('button',name='Поставить следующей',exact=True).click()
            page.wait_for_function('window.__fadeCalls.some(c=>c.type==="queue"&&c.queue.manualNextId)')
            after=rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)');assert after==[original[2],*original[:2],*original[3:]]
            cdp=context.new_cdp_session(page)
            def touch(kind,x=None,y=None):cdp.send('Input.dispatchTouchEvent',{'type':kind,'touchPoints':[] if x is None else [{'x':x,'y':y,'id':1}]})
            rows.first.evaluate('(r)=>r.scrollIntoView({block:"start",behavior:"instant"})');page.evaluate('window.scrollBy({top:-100,behavior:"instant"})')
            first=rows.first.bounding_box();target=rows.nth(2).bounding_box();x=first['x']+85;y=first['y']+first['height']/2
            plays=page.evaluate('window.__fadeCalls.filter(c=>c.type==="play").length')
            touch('touchStart',x,y);page.wait_for_timeout(470);assert rows.first.get_attribute('aria-grabbed')=='true'
            # Native polling may ask for a render while the finger holds a row.
            page.evaluate('window.__queueState.files=[{md5:"qa-file"}];window.__aidjNativeState(structuredClone(window.__queueState))')
            assert rows.first.get_attribute('aria-grabbed')=='true'
            touch('touchMove',x,target['y']+target['height']/2);touch('touchEnd');page.wait_for_timeout(150)
            dragged=rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)');assert dragged==[after[1],after[2],after[0],*after[3:]], {'before':after[:4],'after':dragged[:4],'scroll':page.evaluate('scrollY'),'dragging':page.locator('.dragging').count()}
            assert page.evaluate('window.__fadeCalls.filter(c=>c.type==="play").length')==plays
            assert page.evaluate('window.__queueState.player.queue.currentId')==current
            # Cancellation never changes order. A quick swipe remains scrolling.
            rows.first.evaluate('(r)=>r.scrollIntoView({block:"start",behavior:"instant"})');page.evaluate('window.scrollBy({top:-100,behavior:"instant"})');box=rows.first.bounding_box();x=box['x']+85;y=box['y']+box['height']/2
            touch('touchStart',x,y);page.wait_for_timeout(470);touch('touchMove',x,y+40);touch('touchCancel');page.wait_for_timeout(50)
            assert rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)')==dragged
            page.wait_for_timeout(510);before_scroll=page.evaluate('scrollY')
            touch('touchStart',x,y);touch('touchMove',x,y-30);touch('touchMove',x,y-70);touch('touchEnd');page.wait_for_timeout(150)
            assert rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)')==dragged
            assert page.evaluate('scrollY')>before_scroll
            rows.first.evaluate('(r)=>r.scrollIntoView({block:"start",behavior:"instant"})');page.evaluate('window.scrollBy({top:-100,behavior:"instant"})')
            box=rows.first.bounding_box();x=box['x']+85;y=box['y']+box['height']/2
            touch('touchStart',x,y);page.wait_for_timeout(470)
            before_auto=page.evaluate('scrollY');bottom=page.locator('#mini-player').bounding_box()['y']
            touch('touchMove',x,bottom-5);page.wait_for_timeout(220)
            assert page.evaluate('scrollY')>before_auto
            touch('touchCancel');page.wait_for_timeout(50)
            assert rows.evaluate_all('(rows)=>rows.map(r=>r.dataset.queueEntry)')==dragged
            # Filtering keeps full queue positions and the promote action
            # targets the real next slot, not the first filtered match.
            wanted=page.evaluate('window.__queueState.player.queue.entries.find(e=>e.trackId==="000000000000000000000050").id')
            search=page.get_by_label('Поиск в очереди');search.fill('Song 79')
            assert rows.count()==1 and rows.first.get_attribute('data-queue-entry')==wanted
            rows.first.get_by_role('button',name='Поставить следующей',exact=True).click()
            page.wait_for_function('window.__queueState.player.queue.manualNextId==='+__import__('json').dumps(wanted))
            page.wait_for_function('()=>document.querySelector(".queue-sortable .queue-status").textContent.startsWith("1 · ")')
            search.fill('');assert rows.count()==70 and rows.first.get_attribute('data-queue-entry')==wanted
            assert page.evaluate('window.__queueState.player.queue.currentId')==current
            # A background level-only update reaches an existing native queue,
            # without changing entry order, current excerpt or position.
            playback_entries='JSON.stringify(window.__queueState.player.queue.entries.map(({remixAnchors,remixAcousticAnchors,remixPool,remixCues,...e})=>e))'
            entries_before=page.evaluate(playback_entries)
            tracks[17]['loudness']={'integratedLufs':-9.35,'truePeakDbtp':.33,'method':'ebu-r128-v1'}
            page.locator('#settings').click();page.get_by_role('button',name='Обновить библиотеку',exact=True).click()
            page.wait_for_function('()=>window.__queueState.player.queue.tracks["000000000000000000000012"].loudness?.integratedLufs===-9.35')
            assert page.evaluate(playback_entries)==entries_before
            assert page.evaluate('window.__queueState.player.queue.currentId')==current
            # Polls must not replace visible rows during a scroll gesture.
            page.evaluate('''()=>{window.__scrollRow=document.querySelector('.queue-sortable [data-queue-entry]');document.dispatchEvent(new Event('scroll'));window.__queueState.files=[{md5:'scroll-new-file'}];for(let i=0;i<12;i++)window.__aidjNativeState(structuredClone(window.__queueState));}''')
            assert page.evaluate('window.__scrollRow.isConnected')
            page.wait_for_timeout(240)
            assert not page.evaluate('window.__scrollRow.isConnected')
            # A transition between the last poll and a queue edit wins over the
            # stale edit. The response restores the live epoch immediately.
            page.evaluate('window.__oldPlayer=structuredClone(window.__queueState.player);window.__advanceBeforeQueue=true')
            upcoming=page.evaluate('window.__queueState.player.queue.entries[1].trackId')
            rows.nth(2).get_by_role('button',name='Поставить следующей',exact=True).click()
            page.wait_for_function('()=>window.__queueState.player.queue.playbackEpoch>window.__oldPlayer.queue.playbackEpoch')
            page.wait_for_function('()=>document.querySelector("#mini-title").textContent===window.__queueState.player.queue.tracks[window.__queueState.player.queue.entries[0].trackId].title')
            assert page.evaluate('window.__queueState.player.queue.currentId')!=current
            assert page.evaluate('window.__queueState.player.queue.entries[0].trackId')==upcoming
            assert page.evaluate('window.__queueState.player.time===window.__queueState.player.queue.entries[0].dj.start+2')
            # A late high-uiRevision snapshot from A cannot overwrite B's epoch.
            page.evaluate('window.__oldPlayer.queue.uiRevision=Date.now()+1000000;window.__aidjNativeState({player:window.__oldPlayer})')
            assert page.locator('#mini-title').inner_text()==page.evaluate('window.__queueState.player.queue.tracks[window.__queueState.player.queue.entries[0].trackId].title')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            print('QUEUE_GESTURES_PASSED touch reorder cancel native_poll duplicates next_pin scroll manual_remix sliders full_shuffle_removed')
        finally:context.close();browser.close()


if __name__=='__main__':main()
