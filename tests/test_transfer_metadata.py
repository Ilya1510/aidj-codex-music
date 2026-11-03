import sqlite3
import time
import pytest
from aidj.config import Settings
from aidj.store import connect
from aidj.web_transfer import register,current


def test_transfer_registry_keeps_only_short_lived_local_descriptor(tmp_path):
    db=connect(Settings(state=tmp_path/'state'));token='a'*43
    assert current(db) is None
    register(db,{'port':43210,'token':token,'origin':'https://attacker.invalid'})
    value=current(db)
    assert value['port']==43210 and value['token']==token
    assert 'origin' not in value and time.time()<value['expires']<=time.time()+901
    db.execute('UPDATE native_transfer SET expires=0');db.commit()
    assert current(db) is None


@pytest.mark.parametrize('body',[{'port':80,'token':'a'*43},{'port':99999,'token':'a'*43},{'port':True,'token':'a'*43},{'port':12345,'token':'short'}])
def test_invalid_local_transfer_descriptors_are_rejected(tmp_path,body):
    db=connect(Settings(state=tmp_path/'state'))
    with pytest.raises(ValueError):register(db,body)
