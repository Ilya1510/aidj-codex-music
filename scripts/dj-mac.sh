#!/usr/bin/env bash
# Run from a checkout on the Mac that already has SSH access to the server.
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Этот установщик предназначен для macOS. На Linux используй python -m aidj.dj_remote.' >&2
  exit 1
fi
TASK_PYTHON=''
if command -v python3.12 >/dev/null 2>&1; then
  TASK_PYTHON="$(command -v python3.12)"
elif command -v brew >/dev/null 2>&1; then
  if ! brew list --versions python@3.12 >/dev/null 2>&1; then
    brew install python@3.12
  fi
  TASK_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
else
  echo 'Нужен Python 3.12. Установи его с python.org или через Homebrew: brew install python@3.12' >&2
  exit 1
fi
"$TASK_PYTHON" - <<'PY'
import os,subprocess
free=os.statvfs('.');available=free.f_bavail*free.f_frsize
memory=int(subprocess.check_output(['sysctl','-n','hw.memsize'],text=True))
print(f'Ресурсы Mac: {os.cpu_count()} CPU, {memory/1024**3:.0f} ГБ RAM, {available/1024**3:.1f} ГБ свободно на диске.')
if available<3*1024**3:raise SystemExit('Для окружения, модели и временных файлов нужно хотя бы 3 ГБ свободного места.')
if memory<4*1024**3:raise SystemExit('Для анализа нужно хотя бы 4 ГБ RAM.')
PY
if [[ ! -x .dj-mac-venv/bin/python ]]; then "$TASK_PYTHON" -m venv .dj-mac-venv; fi
TASK_REQUIREMENTS="$("$TASK_PYTHON" - <<'PYHASH'
import hashlib
from pathlib import Path
value=hashlib.sha256()
for name in ('requirements-dj-runtime.txt','requirements-dj-worker.txt'):value.update(Path(name).read_bytes())
print(value.hexdigest())
PYHASH
)"
TASK_INSTALLED=''
if [[ -f .dj-mac-venv/.requirements.sha256 ]]; then TASK_INSTALLED="$(< .dj-mac-venv/.requirements.sha256)"; fi
if [[ "$TASK_INSTALLED" != "$TASK_REQUIREMENTS" ]]; then
  .dj-mac-venv/bin/python -m pip install --disable-pip-version-check -r requirements-dj-worker.txt
  echo "$TASK_REQUIREMENTS" > .dj-mac-venv/.requirements.sha256
fi
# Caffeinate keeps the batch running while the screen is locked. Closing the
# laptop lid may still sleep macOS; another invocation resumes leased jobs.
exec caffeinate -i .dj-mac-venv/bin/python -m aidj.dj_remote "$@"
