from pathlib import Path
import pytest

from aidj.config import Settings,atomic_json
from aidj.webapp import public_url
from deploy.install_services import render,TEMPLATES


def test_units_render_for_another_user_home_and_checkout():
    for template in TEMPLATES.iterdir():
        value=render('/srv/AI DJ','music','/home/music',template.read_text())
        assert '@PROJECT@' not in value and '@HOME@' not in value and '@USER@' not in value
        assert '/home/ilya-grid' not in value
        if template.suffix=='.service':assert 'User=music' in value and '"/srv/AI DJ/.venv/bin/python"' in value


def test_data_paths_are_configurable_without_shell_profile(monkeypatch,tmp_path):
    monkeypatch.setenv('AIDJ_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('AIDJ_CREDENTIALS_DIR',str(tmp_path/'secrets'))
    assert Settings().state==tmp_path/'state' and Settings().credentials==tmp_path/'secrets'


@pytest.mark.parametrize('url,expected',[
 ('https://music.example.org','https://music.example.org'),('https://music.example.org/','https://music.example.org'),
 ('http://music.example.org',''),('https://user:password@music.example.org',''),
 ('https://music.example.org:444',''),('https://music.example.org/path',''),('https://music.example.org?token=x',''),
])
def test_configured_https_origin_supports_stable_domain_without_url_credentials(tmp_path,url,expected):
    settings=Settings(state=tmp_path/'state');settings.prepare();atomic_json(settings.state/'miniapp-url.json',{'url':url})
    assert public_url(settings)==expected


def test_systemd_accepts_rendered_unit_with_spaces_in_checkout(tmp_path):
    import getpass,shutil,subprocess,sys
    if not shutil.which('systemd-analyze'):pytest.skip('systemd not installed')
    project=tmp_path/'AI DJ';binary=project/'.venv/bin/python';binary.parent.mkdir(parents=True);binary.symlink_to(sys.executable)
    unit=tmp_path/'aidj-state-backup.service'
    unit.write_text(render(project,getpass.getuser(),Path.home(),(TEMPLATES/unit.name).read_text()))
    result=subprocess.run(['systemd-analyze','verify',str(unit)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
