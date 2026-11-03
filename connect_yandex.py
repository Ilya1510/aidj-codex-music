"""Import OAuth tokens locally without echoing or logging their values."""
import argparse
import getpass
from aidj.config import Settings,atomic_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--music',action='store_true');parser.add_argument('--disk',action='store_true');parser.add_argument('--replace',action='store_true')
    args=parser.parse_args();settings=Settings()
    selected=[('music','credentials.json','OAuth-токен Яндекс Музыки'),('disk','disk-credentials.json','OAuth-токен Яндекс Диска')]
    for key,file,label in selected:
        if (args.music or args.disk) and not getattr(args,key):continue
        path=settings.credentials/file
        if path.exists() and not args.replace:
            print(key+': уже настроено. --replace заменяет сохранённый токен.');continue
        token=getpass.getpass(label+' (ввод скрыт): ').strip()
        if not token or any(c.isspace() for c in token):raise SystemExit('Пустой или некорректный токен.')
        atomic_json(path,{'access_token':token});path.chmod(0o600)
        print(key+': сохранено локально.')


if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):raise SystemExit('Отменено.')
