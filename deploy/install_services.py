"""Render systemd units for this checkout and its unprivileged service account."""
import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile

TEMPLATES=Path(__file__).resolve().parent/'units'
ACTIVE=('aidj-codex-music.service','aidj-music-web.service','aidj-music-tunnel.service',
        'aidj-youtube-health.timer','aidj-state-backup.timer')


def quoted(value):
    # Values are written to systemd syntax, never executed by a shell.
    value=str(value)
    if any(c in value for c in '\n\r\0'):raise ValueError('Invalid path')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def render(project,user,home,template):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?',user):raise ValueError('Invalid service user')
    text=template.replace('User=@USER@','User='+user)
    quoted(project)  # Validate control characters; WorkingDirectory is not shell-quoted.
    text=text.replace('WorkingDirectory=@PROJECT@','WorkingDirectory='+str(project).replace('%','%%'))
    text=text.replace('Environment=HOME=@HOME@','Environment='+quoted('HOME='+str(home)))
    text=text.replace('ExecStart=@PROJECT@/.venv/bin/python','ExecStart='+quoted(Path(project)/'.venv/bin/python'))
    if '@PROJECT@' in text or '@HOME@' in text or '@USER@' in text:raise ValueError('Unresolved placeholder')
    return text


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--user',required=True);parser.add_argument('--project',type=Path,default=TEMPLATES.parent.parent)
    parser.add_argument('--output',type=Path);parser.add_argument('--install',action='store_true');parser.add_argument('--enable',action='store_true');parser.add_argument('--backup-only',action='store_true')
    args=parser.parse_args();account=pwd.getpwnam(args.user);project=args.project.resolve()
    if not (project/'.venv/bin/python').is_file():raise SystemExit('Create .venv first')
    if args.install and os.geteuid()!=0:raise SystemExit('Use sudo for --install')
    output=Path('/etc/systemd/system') if args.install else args.output
    if output is None:raise SystemExit('Use --output for review, or --install to install')
    output.mkdir(parents=True,exist_ok=True)
    templates=list(TEMPLATES.glob('aidj-state-backup.*')) if args.backup_only else sorted(TEMPLATES.iterdir())
    for file in templates:
        text=render(project,args.user,account.pw_dir,file.read_text())
        fd,name=tempfile.mkstemp(prefix='.'+file.name,dir=output)
        try:
            with os.fdopen(fd,'w') as stream:stream.write(text)
            os.chmod(name,0o644);os.replace(name,output/file.name)
        finally:Path(name).unlink(missing_ok=True)
    if args.install:
        subprocess.run(['systemctl','daemon-reload'],check=True)
        if args.enable:
            units=['aidj-state-backup.timer'] if args.backup_only else list(ACTIVE)
            subprocess.run(['systemctl','enable','--now',*units],check=True)
    print('Systemd units ready:',len(templates))


if __name__=='__main__':main()
