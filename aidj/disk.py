import hashlib
from pathlib import Path
import time
from urllib.parse import quote

import requests


class DiskError(RuntimeError):
    pass


def md5(path):
    digest = hashlib.md5()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(262144), b''):
            digest.update(chunk)
    return digest.hexdigest()


def web_url(path):
    return 'https://disk.yandex.ru/client/disk' + quote(path, safe='/')


class Disk:
    base = 'https://cloud-api.yandex.net/v1/disk'

    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'OAuth ' + settings.secret('disk-credentials.json')['access_token']

    def request(self, method, endpoint, **kwargs):
        for attempt in range(3):
            response = self.session.request(method, self.base + endpoint, timeout=(15, 45), **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            if attempt < 2:
                time.sleep(2**attempt)
        return response

    def metadata(self, path):
        r = self.request('GET', '/resources', params={'path':path, 'fields':'type,path,size,md5'})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise DiskError('Диск не отдал сведения о файле: HTTP ' + str(r.status_code))
        return r.json()

    def items(self, path):
        offset = 0
        while True:
            r = self.request('GET', '/resources', params={'path':path, 'limit':200, 'offset':offset,
                'fields':'_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.md5,_embedded.items.size,_embedded.total'})
            if r.status_code != 200:
                raise DiskError('Не удалось прочитать папку на Диске: HTTP ' + str(r.status_code))
            page = r.json()['_embedded']
            items = page.get('items', [])
            yield from items
            offset += len(items)
            if offset >= page['total']:
                return
            if not items:
                raise DiskError('Диск вернул неполный список файлов.')

    def wait_operation(self, response):
        href = response.json()['href']
        for _ in range(90):
            r = self.session.get(href, timeout=20)
            if r.status_code != 200:
                raise DiskError('Диск не подтвердил операцию.')
            status = r.json().get('status')
            if status == 'success':
                return
            if status == 'failed':
                raise DiskError('Операция на Диске не завершилась.')
            time.sleep(1)
        raise DiskError('Операция на Диске превысила лимит времени.')

    def move(self, original, target):
        r = self.request('POST', '/resources/move', params={'from':original, 'path':target, 'overwrite':'false'})
        if r.status_code == 201:
            return
        if r.status_code == 202:
            return self.wait_operation(r)
        raise DiskError('Не удалось переместить файл на Диске: HTTP ' + str(r.status_code))

    def trash_empty(self, path):
        if next(self.items(path), None) is not None:
            raise DiskError('Папка не пустая; удаление отменено.')
        r = self.request('DELETE', '/resources', params={'path':path, 'permanently':'false'})
        if r.status_code == 204:
            return
        if r.status_code == 202:
            return self.wait_operation(r)
        raise DiskError('Не удалось убрать пустую папку: HTTP ' + str(r.status_code))

    def mkdir(self, path):
        r = self.request('PUT', '/resources', params={'path':path})
        if r.status_code == 409:
            if (self.metadata(path) or {}).get('type') != 'dir':
                raise DiskError('Путь на Диске занят файлом.')
        elif r.status_code != 201:
            raise DiskError('Не удалось создать папку на Диске: HTTP ' + str(r.status_code))

    def verify(self, path, digest, size):
        item = self.metadata(path)
        return bool(item and item.get('md5') == digest and item.get('size') == size)

    def upload(self, local, remote, overwrite=False):
        local = Path(local)
        digest, size = md5(local), local.stat().st_size
        existing = self.metadata(remote)
        if existing and existing.get('md5') == digest and existing.get('size') == size:
            return digest
        if existing and not overwrite:
            raise DiskError('Такой файл уже есть на Диске с другим содержимым.')
        r = self.request('GET', '/resources/upload', params={'path':remote, 'overwrite':str(overwrite).lower()})
        if r.status_code != 200:
            raise DiskError('Диск не разрешил загрузку: HTTP ' + str(r.status_code))
        href = r.json()['href']
        for attempt in range(3):
            with local.open('rb') as f:
                uploaded = requests.put(href, data=f, timeout=(15,180), headers={'Content-Type':'application/octet-stream'})
            if uploaded.status_code in (201,202):
                break
            if uploaded.status_code not in (429,500,502,503,504) or attempt == 2:
                raise DiskError('Загрузка на Диск не прошла: HTTP ' + str(uploaded.status_code))
            time.sleep(2**attempt)
        for delay in (0,1,2,4,8,16):
            if delay:
                time.sleep(delay)
            if self.verify(remote, digest, size):
                return digest
        raise DiskError('Диск не подтвердил сохранность загруженного файла.')

    def download(self, remote, local, expected_md5=None):
        r = self.request('GET', '/resources/download', params={'path':remote})
        if r.status_code != 200:
            raise DiskError('Диск не разрешил скачивание: HTTP ' + str(r.status_code))
        size = 0
        try:
            with requests.get(r.json()['href'], stream=True, timeout=(15,45)) as response:
                if response.status_code != 200:
                    raise DiskError('Не удалось получить файл с Диска.')
                with Path(local).open('wb') as f:
                    for chunk in response.iter_content(131072):
                        size += len(chunk)
                        if size > self.settings.max_bytes:
                            raise DiskError('Файл превышает лимит размера.')
                        f.write(chunk)
            if expected_md5 and md5(local) != expected_md5:
                raise DiskError('Содержимое файла изменилось с прошлого расчёта.')
        except BaseException:
            Path(local).unlink(missing_ok=True)
            raise

    def copy(self, original, target):
        r = self.request('POST', '/resources/copy', params={'from':original, 'path':target, 'overwrite':'false'})
        if r.status_code == 201:
            return
        if r.status_code != 202:
            raise DiskError('Не удалось сохранить исходник: HTTP ' + str(r.status_code))
        self.wait_operation(r)
