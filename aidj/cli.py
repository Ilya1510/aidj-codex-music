"""Local smoke test of the same pipeline used by the Telegram worker."""
import argparse
import json
from pathlib import Path
import tempfile

from .config import Settings
from .locking import worker_lock
from .pipeline import Pipeline,SelectionNeeded
from .store import connect


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('query')
    args=parser.parse_args()
    settings=Settings();db=connect(settings)
    try:
        with worker_lock(settings):
            with tempfile.TemporaryDirectory(prefix='smoke-',dir=settings.state/'work') as work:
                result=Pipeline(settings,db).run(args.query,{},Path(work),lambda text:print(text,flush=True))
                result.pop('local_path',None)
                result.pop('telegram_file_id',None)
                print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except SelectionNeeded as selection:
        print('SELECTION_NEEDED',json.dumps([t.to_dict() for t in selection.tracks],ensure_ascii=False))
        return 2
    except Exception as exc:
        print('SMOKE_FAILED',type(exc).__name__)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
