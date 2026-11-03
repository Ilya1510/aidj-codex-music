from contextlib import contextmanager
import fcntl


@contextmanager
def worker_lock(settings):
    settings.prepare()
    with (settings.state/'worker.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)
