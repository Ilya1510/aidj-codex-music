# Обслуживание

## Проверки и журналы

```bash
curl --fail http://127.0.0.1:18782/health
systemctl is-active aidj-codex-music.service aidj-music-web.service aidj-music-tunnel.service
systemctl list-timers 'aidj-*'
journalctl -u aidj-codex-music.service -n 30 --no-pager
journalctl -u aidj-state-backup.service -n 20 --no-pager
```

Не включай HTTP verbose/debug и не публикуй приватные настройки или всю БД в issue.
При сбое systemd проверь также `df -h`, `free -m`, memory pressure и лимиты задания.
Перезапуск туннеля может сменить HTTPS-адрес; без необходимости его не перезапускай.

## Резервное копирование

```bash
.venv/bin/python -m aidj.backup upload
```

Папка на Диске — `/Музыка/_aidj_state/`. Музыкальные файлы в снимок не дублируются.
Таймер запускается раз в час. Приложение получает изменения из рабочей базы сервера
независимо от бэкапа: при открытом приложении проверка идёт примерно раз в 20 секунд.
Снимки копируются по 100 строк, файлы передаются потоково. Лимит снимка — 128 MiB;
нехватка места или превышение лимита завершает задание без замены прежнего бэкапа.
Занятая музыкальная очередь откладывает попытку. Неизменившийся снимок повторно
не загружается, кроме первого дневного сохранения.

Установка только новых backup-служб на действующую VM, без перезапуска туннеля:

```bash
sudo python3 deploy/install_services.py --user aidj --project /home/aidj/aidj-codex-music \
  --install --enable --backup-only
sudo systemctl start aidj-state-backup.service
```

В `latest.json` есть время снимка, контрольные суммы и числа записей. Периодически
проверяй **восстановление в отдельную папку**, не только факт успешной загрузки.

## Обновление сервера

```bash
git pull --ff-only
.venv/bin/pip install -e .
.venv/bin/python -m pytest tests -q
```

Перед рестартом дождись завершения активных заданий. Затем перезапусти только
изменённые службы, например бота и web. Не запускай второй экземпляр poller с тем
же токеном. Перед существенными миграциями сохрани свежую копию данных.

## Android

Сборка не использует Gradle и эмулятор. Установи инструменты один раз:

```bash
.venv/bin/python -m tests.setup_android_tools
```

Сохраняй `android-signing/aidj.p12` и `android-signing/identity.json` в отдельном
приватном хранилище. Не размещай их в GitHub, в общих папках или в issue.
Сборщик не заменяет существующий ключ и проверяет подпись относительно прежнего релиза.

Пример ограниченной сборки; замени пользователя и пути:

```bash
sudo systemd-run --unit=aidj-apk-build --collect --wait --pipe --uid=aidj \
  --working-directory=/home/aidj/aidj-codex-music \
  -p MemoryMax=1G -p MemoryLimit=1G -p TasksMax=64 -p RuntimeMaxSec=240 \
  -p Nice=10 -p BlockIOWeight=10 \
  /home/aidj/aidj-codex-music/.venv/bin/python android/build.py
.venv/bin/python android/publish.py
```

Перед изменением опубликованной APK увеличь `versionCode` и `versionName` в
`android/AndroidManifest.xml`. Публикация проверяет подпись, хеш, размер и отсутствие
рабочих токенов. Каждый релиз имеет неизменяемый URL; старые версии не перезаписываются.
Бот и Mini App получают текущую версию через `/api/android-version`.

## Проверки интерфейса

```bash
.venv/bin/pip install -r requirements-browser.txt
.venv/bin/playwright install --with-deps chromium --only-shell
.venv/bin/python -m tests.browser_reviews
.venv/bin/python -m tests.browser_playlist_changes
```

Используется Chromium, установленный Playwright; `AIDJ_CHROMIUM` задаёт альтернативный
бинарник. На минимальной VM без системных библиотек есть необязательный
`tests/fetch_browser_libs.py`; его `browser-libs/root/usr/lib/x86_64-linux-gnu`
можно добавить к `LD_LIBRARY_PATH` только для QA-процесса.

Браузерные проверки запускай по одной в отдельном cgroup с MemoryMax/MemoryLimit=1G,
TasksMax=64 и таймаутом. Часть старых smoke-тестов читает действующий локальный сервер;
`tests.browser_reviews` использует отдельный сервер и синтетические оценки.

## YouTube

Проверка выполняет одну реальную пробную загрузку в день и при необходимости
уведомляет владельца. Она не складывает тестовое аудио в библиотеку.
Для обновления cookies используй `connect_youtube.py`; изменение библиотеки
загрузки само по себе не гарантирует устранение требования входа.

## Сторонние компоненты

`miniapp/telegram-web-app.js` — SDK Telegram из `https://telegram.org/js/telegram-web-app.js`.
У него сохраняются upstream-условия. Python-зависимости перечислены отдельно,
Node и cloudflared загружаются из официальных релизов с проверкой контрольных сумм.
