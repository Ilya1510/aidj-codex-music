"""Only temporary connection metadata; music bytes never pass through the VM."""
import json
import re
import time


def register(db,body):
    port=body.get('port');token=body.get('token','')
    if type(port) is not int or not 1024<=port<=65535 or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}',token):
        raise ValueError('invalid transfer descriptor')
    descriptor={'port':port,'token':token,'expires':int(time.time()+900)}
    db.execute('INSERT INTO native_transfer VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET descriptor=excluded.descriptor,expires=excluded.expires',
               (json.dumps(descriptor),descriptor['expires']));db.commit()


def current(db):
    row=db.execute('SELECT descriptor FROM native_transfer WHERE id=1 AND expires>?',(time.time(),)).fetchone()
    return json.loads(row[0]) if row else None
