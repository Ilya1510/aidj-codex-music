"""A sparse song stays in remix at the selected length; old APKs keep their protocol."""
from playwright.sync_api import sync_playwright
from tests.browser_playback import BRIDGE,launch
from tests.browser_release20 import catalog


def main():
    data=catalog();data['tracks']=data['tracks'][:2]
    data['playlists']=data['playlists'][:1];data['playlists'][0]['trackIds']=[t['id'] for t in data['tracks']]
    for t in data['tracks']:
        t['duration']=112.744;t['djV4']={'version':3,'cues':[],'remix':[],
            'anchors':{'version':1,'wordGuarded':True,'bpm':0,'entries':[[0,.8],[17.373,.8]],'exits':[[21.633,.6,.8]]}}
    with sync_playwright() as p:
        for version in (25,24):
            errors=[];browser=launch(p);context=browser.new_context(viewport={'width':390,'height':844},service_workers='block')
            context.add_init_script(BRIDGE.replace('version:()=>8',f'version:()=>{version}'))
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            def api(route):
                route.fulfill(json=data if route.request.url.endswith('/catalog') else {'ok':True,'revision':'qa25','queued':0})
            page.route('**/api/**',api)
            try:
                page.goto('http://127.0.0.1:18782',wait_until='networkidle')
                page.locator('.playlist-card').filter(has_text='Volleyball').click()
                page.get_by_role('button',name='Воспроизведение · Обычное',exact=True).click()
                page.get_by_role('button',name='DJ: ремикс',exact=True).click()
                page.get_by_role('button',name='Закрыть',exact=True).click()
                page.get_by_role('button',name='Слушать',exact=True).click()
                page.wait_for_function('window.__fadeCalls.some(x=>x.type==="play")')
                result=page.evaluate('''()=>{const q=window.__fadeCalls.filter(x=>x.type==='play').at(-1).queue;
                    return {entries:q.entries,target:q.remixDurationSeconds,current:q.entries.find(e=>e.id===q.currentId)};}''')
                assert len(result['entries'])==2 and result['target']==12,result
                cue=result['current'].get('dj')
                if version==25:
                    assert cue and cue['timedFallback'] and cue['quality']=='timed' and cue['needsReview'],result
                    assert not cue['entryVerified'] and not cue['wordGuarded'] and not cue.get('acoustic'),result
                    assert abs(cue['end']-cue['start']-12)<.01,result
                    page.locator('[data-view="queue"]').click()
                    page.locator('.dj-queue-cue').filter(has_text='примерный переход').first.wait_for()
                else:
                    assert not cue or not cue.get('timedFallback'),result
                assert not errors,errors
            finally:context.close();browser.close()
    print('RELEASE25_UI_PASSED sparse_song_duration honest_caption old_apk_compatibility',flush=True)


if __name__=='__main__':main()
