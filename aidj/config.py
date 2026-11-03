from dataclasses import dataclass,field
from pathlib import Path
import json
import os
import tempfile


@dataclass
class Settings:
    state: Path = field(default_factory=lambda:Path(os.environ.get('AIDJ_STATE_DIR',Path.home()/'.local/share/aidj-codex-music')).expanduser())
    credentials: Path = field(default_factory=lambda:Path(os.environ.get('AIDJ_CREDENTIALS_DIR',Path.home()/'.local/share/yandex-music-export')).expanduser())
    disk_folder: str = '/Музыка/aidj_codex_music'
    max_bytes: int = 40 * 1024**2
    max_duration: int = 1800

    def prepare(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        (self.state / 'work').mkdir(exist_ok=True, mode=0o700)

    def secret(self, name):
        return json.loads((self.credentials / name).read_text())


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
