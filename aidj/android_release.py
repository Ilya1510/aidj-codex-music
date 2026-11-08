import json
from urllib.parse import urlencode
from .web_auth import browser_code


def version(settings,origin):
    path=settings.state/'android-release/version.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    value['url']=origin+f'/android/releases/{value["versionCode"]}/AI-DJ.apk'
    return value


def connection_url(db,origin,*,role='admin',user_id=None):
    return origin+'/android-connect#'+urlencode({'server':origin,'code':browser_code(db,role=role,user_id=user_id)})
