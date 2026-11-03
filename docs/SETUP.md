# Новый сервер

Если нужно вернуть действующие плейлисты и оценки, сначала прочитай
[RECOVERY.md](RECOVERY.md). Запуск пустой базы не восстанавливает историю с Диска.

## 1. Пользователь и код

Пример для Ubuntu 24.04 x86_64. Сервер, бот и сборка работают от отдельного
обычного пользователя `aidj`. `sudo` нужен только для пакетов и systemd.

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git ca-certificates
sudo adduser --disabled-password --gecos '' aidj
sudo -iu aidj

git clone https://github.com/Ilya1510/aidj-codex-music.git
cd aidj-codex-music
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/python deploy/install_runtime_tools.py
```

Установщик проверяет суммы Node и cloudflared. Системный Node >= 20 можно
использовать вместо скачанного; по умолчанию ставится Node 24. Каталог с инструментами
проекта независим от других проектов на VM. FFmpeg приходит с `imageio-ffmpeg`.

Состояние по умолчанию — `~/.local/share/aidj-codex-music`, данные входа в Яндекс —
`~/.local/share/yandex-music-export`. Можно задать абсолютные пути через
`AIDJ_STATE_DIR` и `AIDJ_CREDENTIALS_DIR`; одинаковые значения нужны всем службам.
`AIDJ_NODE` и `AIDJ_CLOUDFLARED` переопределяют пути бинарников.

## 2. Подключения

Секреты вводятся на сервере в скрытый prompt. Не передавай их параметрами команды,
не добавляй в Git и не присылай боту сообщением.

### Яндекс

```bash
.venv/bin/python connect_yandex.py
```

Нужны собственные OAuth-токены Музыки и Диска. Для Диска — своё приложение в
[Яндекс OAuth](https://oauth.yandex.ru/) с чтением и записью файлов и информацией о Диске.
Помощник сохраняет только локальные файлы `credentials.json` и `disk-credentials.json`.
Уже сохранённые значения без `--replace` не меняются.

При переносе со старой VM можно восстановить эти два файла из **отдельной приватной
копии**, сохранив владельца и права 0600. Музыкальные файлы останутся на прежнем Диске.
Если токенов не осталось, потребуется заново авторизовать аккаунты; резервная копия
оценок токенов не содержит.

### Telegram

Для восстановления используй **тот же бот и токен BotFather**. При новом проекте
создай бота с `aidj` в username.

```bash
.venv/bin/python connect_telegram.py
```

Открой выданную одноразовую ссылку в своём Telegram: это закрепляет управление
за владельцем. Ссылка предназначена только владельцу. Для друзей бот отдельно
выдаёт `/reviewlink`. Одновременно должен работать только один poller этого бота.

### YouTube

```bash
.venv/bin/python connect_youtube.py
```

Войди в YouTube в отдельном окне инкогнито, в той же вкладке открой
`https://www.youtube.com/robots.txt`, экспортируй cookies YouTube, закрой окно
и вставь экспорт в помощник. Достаточно Enter; окончания `END` нет.
Cookies сохраняются локально и со временем могут потребовать обновления.

## 3. Службы

Вернись в исходную учётную запись с sudo (`exit`, если выше открывал `sudo -iu aidj`).
Укажи реальный каталог checkout:

```bash
sudo python3 deploy/install_services.py \
  --user aidj --project /home/aidj/aidj-codex-music --install --enable
```

Для предварительного просмотра вместо `--install --enable` используй
`--output /tmp/aidj-units` и проверь файлы. Установщик сам подставляет пользователя,
домашний каталог и путь Python. На VM с cgroup v1 и v2 заданы жёсткие лимиты памяти.

Службы:

- `aidj-codex-music.service` — бот, последовательная очередь обработки и уведомления.
- `aidj-music-web.service` — API и интерфейс, только `127.0.0.1:18782`.
- `aidj-music-tunnel.service` — временный HTTPS-адрес Cloudflare.
- `aidj-youtube-health.timer` — проверка загрузки ежедневно в 10:00 Europe/Moscow.
- `aidj-state-backup.timer` — копирование данных на Диск каждую минуту при изменениях.

```bash
systemctl is-active aidj-codex-music.service aidj-music-web.service aidj-music-tunnel.service
curl --fail http://127.0.0.1:18782/health
systemctl list-timers 'aidj-*'
```

Порты базы и API не публикуются напрямую. Доступ к API приложения разрешён только
владельцу Telegram. Гостевой режим друзей работает через бота, а не через приложение.

## 4. HTTPS и Android

Быстрый туннель запишет адрес в `miniapp-url.json`, а бот обновит свою кнопку «Музыка».
Адрес может измениться после перезапуска туннеля. Для постоянного развёртывания
подключи свой стабильный HTTPS-домен через reverse proxy к `127.0.0.1:18782`, выключи
`aidj-music-tunnel.service` и запиши в файл состояния:

```json
{"url": "https://music.example.org"}
```

Сервер принимает HTTPS-origin без пути, логина, query и нестандартного порта.
Reverse proxy должен пропускать Range-запросы аудио. После изменения адреса
перезапусти только бота и web-службу; `/android` выдаст новое подключение приложения.

Подключения и сборку запускай от пользователя `aidj` (`sudo -iu aidj`, затем
`cd ~/aidj-codex-music`), чтобы не создать состояние и ключи в `/root`.
Для APK нужен JDK/Android SDK:

```bash
.venv/bin/python -m tests.setup_android_tools
.venv/bin/python android/build.py
.venv/bin/python android/publish.py
```

На небольшой VM запускай сборку в отдельном cgroup с лимитом памяти, см.
[OPERATIONS.md](OPERATIONS.md). При восстановлении **сначала верни старый
`android-signing/`**, затем собирай. Создание нового ключа не позволит обновить
установленное приложение поверх старого.
