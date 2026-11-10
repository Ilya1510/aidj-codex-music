"""Graph shuffle preserves the live two-deck prefix and silent paused state."""
import io
import os
import wave

from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE, launch
from aidj.webapp import byte_range


def fixture():
    tracks = [{'id': f'{i+1:024x}', 'md5': f'{i+1:032x}', 'title': 'Graph '+str(i),
               'artist': 'Artist '+str(i), 'duration': 30., 'bytes': 100, 'format': 'wav'} for i in range(8)]
    rows = [{'trackId': t['id'], 'md5': t['md5'], 'start': 1., 'end': 21., 'fadeIn': 5.,
             'fadeOut': .4, 'rate': 1., 'transitionSeconds': 5. if i<7 else 0.,
             'transitionKind': 'crossfade' if i<7 else 'clean-cut'} for i, t in enumerate(tracks)]
    graph = {'version': 1, 'revision': 'a'*20, 'baseRevision': 'b'*20, 'degree': 7,
             'artists': {t['id']: ['artist'+str(i)] for i, t in enumerate(tracks)}, 'edges': {}}
    for i, left in enumerate(tracks):
        graph['edges'][left['id']] = [{'to': right['id'], 'score': abs(i-j), 'beatMatched': False,
            'transitionSeconds': 0. if i==7 and j==0 else 5.,
            'transitionKind': 'clean-cut' if i==7 and j==0 else 'crossfade'}
            for j, right in enumerate(tracks) if j!=i]
    frozen = {'id': 'dj1', 'name': 'DJ 1', 'revision': graph['baseRevision'], 'playlistId': 1,
              'tracks': rows, 'graph': graph}
    return {'access': {'role': 'admin', 'userId': 1}, 'tracks': tracks, 'djSets': [frozen],
            'playlists': [{'id': 1, 'name': 'Volleyball', 'trackIds': [t['id'] for t in tracks], 'revision': 'qa'}]}


def main():
    catalog = fixture()
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000);stream.writeframes(b'\0\0'*(30*8000))
    body = wav.getvalue()
    def api(route):
        path = route.request.url.split('/api/', 1)[1]
        if path.startswith('audio/'):
            start, end, partial = byte_range(route.request.headers.get('range'), len(body))
            headers = {'Accept-Ranges': 'bytes'}
            if partial:headers['Content-Range'] = f'bytes {start}-{end}/{len(body)}'
            route.fulfill(status=206 if partial else 200, body=body[start:end+1], headers=headers, content_type='audio/wav')
        elif path == 'catalog':route.fulfill(json=catalog)
        elif path == 'android-version':route.fulfill(status=503, json={'error': 'QA'})
        else:route.fulfill(json={'ok': True, 'revision': 'qa', 'access': catalog['access']})
    with sync_playwright() as p:
        for native in (True, False):
            browser = launch(p);ctx = browser.new_context(viewport={'width': 412, 'height': 915}, service_workers='block')
            if native:
                ctx.add_init_script(BRIDGE.replace('version:()=>8', 'version:()=>42').replace('const state={player:', 'const state=window.__graphState={player:'))
            else:
                ctx.add_init_script('''window.__graphDecks=[];window.__graphPlays=0;
                  const RealAudio=window.Audio;window.Audio=function(...args){const a=new RealAudio(...args);window.__graphDecks.push(a);return a;};
                  const originalPlay=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){window.__graphPlays++;return originalPlay.apply(this,args);};''')
            page = ctx.new_page();page.set_default_timeout(10000);errors = [];page.on('pageerror', lambda error:errors.append(str(error)));page.route('**/api/**', api)
            try:
                page.goto(os.environ.get('AIDJ_QA_ORIGIN', 'http://127.0.0.1:18787'), wait_until='networkidle')
                page.locator('.playlist-card').click()
                assert page.get_by_role('button', name='Перемешать DJ 1', exact=True).count()==1
                page.get_by_role('button', name='Слушать DJ 1', exact=True).click()
                page.wait_for_function('()=>document.querySelector("#mini-title").textContent==="Graph 0"')
                if native:
                    page.evaluate('window.__graphState.player.time=6;window.__graphState.player.duration=30;window.__aidjNativeState(structuredClone(window.__graphState))')
                else:
                    page.wait_for_function('()=>!document.querySelector("audio").paused')
                    page.evaluate('document.querySelector("audio").currentTime=6;window.__graphMain=document.querySelector("audio")')
                page.locator('.bottom-nav [data-view="queue"]').click()
                assert page.get_by_role('button', name='Перемешать', exact=True).count()==1
                page.wait_for_function('async()=>!!(await(await import("/storage.mjs")).meta("player"))?.currentId')
                before = page.evaluate('async()=>await(await import("/storage.mjs")).meta("player")')
                if native:before = page.evaluate('window.__graphState.player.queue')
                page.get_by_role('button', name='Перемешать', exact=True).click()
                page.wait_for_function('async()=>!!(await(await import("/storage.mjs")).meta("player"))?.fixedSet?.routeId')
                first = page.evaluate('async()=>await(await import("/storage.mjs")).meta("player")')
                assert first['currentId']==before['currentId'] and first['entries'][:2]==before['entries'][:2]
                assert len(first['entries'])==8 and first['fixedSet']['route']=='graph'
                assert 'edges' not in first['fixedSet'] and 'graph' not in first['fixedSet']
                if native:
                    assert page.evaluate('window.__graphState.player.time')==6
                else:
                    assert page.evaluate('document.querySelector("audio")===window.__graphMain&&!window.__graphMain.paused&&window.__graphMain.currentTime>=6&&window.__graphMain.currentTime<7')
                page.locator('#mini-play').click()
                if native:page.wait_for_function('()=>window.__graphState.player.paused')
                else:page.wait_for_function('()=>document.querySelector("audio").paused')
                if native:count = page.evaluate('window.__fadeCalls.length')
                else:count = page.evaluate('window.__graphPlays')
                route_id = first['fixedSet']['routeId']
                page.get_by_role('button', name='Перемешать', exact=True).click()
                page.wait_for_function('(id)=>document.querySelector("#toast").textContent.includes("новый маршрут")&&(window.AndroidMusic?JSON.parse(AndroidMusic.readMeta("player")):null)?.fixedSet?.routeId!==id' if native else 'async(id)=>(await(await import("/storage.mjs")).meta("player"))?.fixedSet?.routeId!==id', arg=route_id)
                page.wait_for_timeout(150)
                if native:
                    calls = page.evaluate('(at)=>window.__fadeCalls.slice(at).map(c=>c.type)', count)
                    assert 'play' not in calls and 'pause' not in calls and 'seek' not in calls, calls
                    assert page.evaluate('window.__graphState.player.paused&&window.__graphState.player.time===6')
                    # A failed queue command must reconcile from Android's own
                    # state, rather than persisting an optimistic route or
                    # suppressing the following retry with its cached key.
                    old_route = page.evaluate('window.__graphState.player.queue.fixedSet.routeId')
                    page.evaluate('''()=>{const request=AndroidMusic.request.bind(AndroidMusic);window.__rejectGraphQueue=true;
                      AndroidMusic.request=(id,raw)=>{const value=JSON.parse(raw);if(value.type==='queue'&&window.__rejectGraphQueue){window.__rejectGraphQueue=false;window.__fadeCalls.push(value);window.__aidjNativeReply(id,{error:'QA graph queue rejected'});return;}request(id,raw);};}''')
                    page.get_by_role('button', name='Перемешать', exact=True).click()
                    page.wait_for_function('()=>document.querySelector("#toast").textContent==="QA graph queue rejected"')
                    assert page.evaluate('window.__graphState.player.queue.fixedSet.routeId')==old_route
                    assert page.evaluate('JSON.parse(AndroidMusic.readMeta("player")).fixedSet.routeId')==old_route
                    assert page.evaluate('window.__graphState.player.paused&&window.__graphState.player.time===6')
                    page.get_by_role('button', name='Перемешать', exact=True).click()
                    page.wait_for_function('(id)=>window.__graphState.player.queue.fixedSet.routeId!==id', arg=old_route)
                    assert page.evaluate('window.__graphState.player.paused&&window.__graphState.player.time===6')
                else:
                    assert page.evaluate('(n)=>window.__graphPlays===n&&document.querySelector("audio").paused&&window.__graphDecks.every(a=>a.paused&&a.volume===0)', count)
                    # Shuffle during an actual overlap must retain both audible
                    # decks and their source positions, not restart either one.
                    page.locator('#mini-play').click()
                    page.wait_for_function('()=>!document.querySelector("audio").paused')
                    page.evaluate('document.querySelector("audio").currentTime=16.3')
                    page.wait_for_function('()=>window.__graphDecks.some(a=>!a.paused&&a.currentTime>1.1)')
                    playing = page.evaluate('''()=>{window.__graphIncoming=window.__graphDecks.find(a=>!a.paused);return {main:document.querySelector('audio').currentTime,next:window.__graphIncoming.currentTime};}''')
                    page.get_by_role('button', name='Перемешать', exact=True).click()
                    page.wait_for_timeout(150)
                    assert page.evaluate('(p)=>document.querySelector("audio")===window.__graphMain&&!window.__graphMain.paused&&!window.__graphIncoming.paused&&window.__graphMain.currentTime>=p.main&&window.__graphIncoming.currentTime>=p.next&&window.__graphMain.currentTime<p.main+1&&window.__graphIncoming.currentTime<p.next+1', playing)
                    page.locator('#mini-play').click()
                    assert page.evaluate('document.querySelector("audio").paused&&window.__graphDecks.every(a=>a.paused&&a.volume===0)')
                assert not errors, errors
            finally:ctx.close();browser.close()
    print('FIXED_GRAPH_PASSED native_queue_only silent_paused_shuffle live_decks_preserved exact_prefix source_rates published_edges', flush=True)


if __name__ == '__main__':main()
