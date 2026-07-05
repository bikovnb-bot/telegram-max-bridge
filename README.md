# telegram-max-bridge

[![Test and deploy](https://github.com/bikovnb-bot/telegram-max-bridge/actions/workflows/deploy.yml/badge.svg)](https://github.com/bikovnb-bot/telegram-max-bridge/actions/workflows/deploy.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Пересылает сообщения из группы Telegram в мессенджер MAX (группу или личку) и
обратно.

Со стороны Telegram используется обычный бот (Telegram Bot API).
Со стороны MAX — личный аккаунт через неофициальную библиотеку
[PyMax](https://github.com/MaxApiTeam/PyMax), т.к. официальный Bot API MAX
доступен только юридическим лицам.

⚠️ Автоматизация личного аккаунта MAX через неофициальный протокол может
нарушать пользовательское соглашение MAX и потенциально привести к блокировке
аккаунта. Используйте на свой риск.

## ⚠️ Важно: где размещать сервер

Telegram Bot API (`api.telegram.org`) на некоторых хостингах (особенно в РФ)
периодически блокируется/сильно душится провайдером на уровне DPI — мост при
этом будет постоянно ловить таймауты при получении сообщений
(`TelegramNetworkError: Request timeout`), даже если MAX работает нормально.

Проверить заранее:
```bash
curl -v --max-time 10 https://api.telegram.org
```
Если зависает/таймаутит — это уже проблема сети хостинга, не кода.

Варианты решения:
1. **Разместить сервер там, где Telegram доступен без блокировок**
   (рекомендуется — так и сделали в этом проекте: перенос на другой VPS
   полностью решил проблему, без изменений в коде)
2. **Пустить трафик бота через прокси**, если сервер обязательно должен быть
   в проблемном регионе — в коде сейчас это **не реализовано**, потребуется
   добавить SOCKS5-прокси в `aiogram.Bot` (пакет `aiohttp-socks`,
   `AiohttpSession(proxy=...)`) и поднять сам прокси (пример конфига для
   xray-клиента — `deploy/xray-client-config.example.json`)

MAX (`api.oneme.ru`) в наблюдаемой практике таких блокировок не имел — но на
всякий случай ту же проверку стоит повторить и для него перед деплоем.

## Содержание

- [Важно: где размещать сервер](#️-важно-где-размещать-сервер)
- [Установка](#установка)
- [Как узнать MAX_TARGET_CHAT_ID](#как-узнать-max_target_chat_id)
- [Запуск моста](#запуск-моста)
- [Двусторонняя пересылка (MAX → Telegram)](#двусторонняя-пересылка-max--telegram)
- [Мониторинг хоста](#мониторинг-хоста)
- [Тесты](#тесты)
- [Веб-интерфейс](#веб-интерфейс-просмотр-статуса-и-правка-настроек)
- [HTTP API для прямой пересылки](#http-api-для-прямой-пересылки-в-max-в-обход-telegram-группы)
- [Деплой на Linux VPS](#деплой-на-linux-vps-systemd--пошагово)
- [Лицензия](#лицензия)

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполните `.env`:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather, бота нужно добавить в исходную группу
- `TELEGRAM_SOURCE_CHAT_ID` — id группы-источника
- `MAX_PHONE` — номер телефона аккаунта MAX, от имени которого будут отправляться сообщения
- `MAX_TARGET_CHAT_ID` — id чата/группы/пользователя в MAX, куда пересылать (см. ниже)

## Как узнать MAX_TARGET_CHAT_ID

```bash
python -m bridge.list_max_chats
```

При первом запуске PyMax запросит код из SMS в консоли, затем выведет список
чатов аккаунта с их id. Возьмите нужный id и впишите в `MAX_TARGET_CHAT_ID`.

## Запуск моста

```bash
python -m bridge.main
```

Сессия MAX сохраняется в `MAX_WORK_DIR` (sqlite), повторный ввод SMS-кода не
требуется при последующих запусках.

Если вход по SMS/push не проходит, используйте вход по QR: `python -m bridge.list_max_chats_qr`
(тот же QR-логин используется в `bridge/main.py`, т.к. он основан на `WebClient`).

## Двусторонняя пересылка (MAX → Telegram)

Мост пересылает не только Telegram → MAX, но и ответы из MAX обратно в тот же
Telegram-чат по тому же маршруту. Свои же сообщения (отправленные самим
мостом) не эхуются обратно — сверяется id отправителя с залогиненным
MAX-аккаунтом. Отключается через `REVERSE_FORWARD_ENABLED=false` в `.env`
или на вкладке "Настройки" веб-интерфейса.

## Мониторинг хоста

Мост периодически проверяет свободное место на диске и память сервера
(`HOST_MONITOR_INTERVAL_SECONDS`, по умолчанию раз в 5 минут) и шлёт алерт в
`ALERT_CHAT_ID` при превышении порогов `DISK_ALERT_PERCENT`/`MEM_ALERT_PERCENT`
(по умолчанию 90%). Повторный алерт по тому же порогу придёт только после
возврата значения ниже порога — чтобы не спамить. Отключается через
`HOST_MONITOR_ENABLED=false`.

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Покрыты: парсинг/сериализация маршрутов, `RateLimiter`, обратная пересылка
MAX → Telegram (включая защиту от эха и rate limiting), мониторинг хоста,
HTTP API (`/forward`, `/chats`). GitHub Actions запускает эти тесты на каждый
push/PR и **не деплоит на сервер, если тесты не прошли** (см. `deploy` job
зависит от `test` job в `.github/workflows/deploy.yml`).

## Веб-интерфейс (просмотр статуса и правка настроек)

Добавьте в `.env`:
```
WEBUI_USERNAME=admin
WEBUI_PASSWORD=<придумайте пароль>
WEBUI_PORT=8765
```

Запуск:
```bash
python -m bridge.webui
```

Откройте `http://<сервер>:8765` — попросит логин/пароль (Basic Auth). Интерфейс
разбит на вкладки:

- **Обзор** — статус подключения к MAX, последние пересланные сообщения, хвост лога
- **Маршруты** — таблица "Telegram Chat ID → MAX Chat ID", кнопка "Показать чаты MAX"
  (подтягивает список чатов из уже залогиненного аккаунта MAX, "Выбрать" сразу
  добавляет маршрут), кнопка "Отправить тестовое сообщение" для проверки без
  необходимости писать в Telegram/на сайте
- **Настройки** — токен бота, алерты о сбоях, антифлуд
- **Обслуживание** — статус systemd-сервисов и кнопки перезапуска (см. ниже про
  `bridge-sudoers`, без него кнопки вернут ошибку)
- **Инструкция** — подробный пошаговый гайд с нуля (создание бота, добавление в
  группу, получение id, частые проблемы) — рассчитан на пользователя, который
  впервые видит этот интерфейс

Для кнопок "Показать чаты MAX"/"Отправить тестовое сообщение" нужен настроенный
`FORWARD_TOKEN`/`FORWARD_PORT` (см. раздел про HTTP API ниже) — веб-интерфейс
обращается к мосту через тот же внутренний API.

### Кнопки перезапуска сервисов (вкладка "Обслуживание")

Веб-интерфейс запущен от отдельного системного пользователя (`bridge`), у
которого по умолчанию нет прав перезапускать systemd-сервисы. Чтобы кнопки
заработали, установите ограниченное правило sudo (разрешает **только**
перезапуск/проверку статуса этих двух сервисов, ничего больше):

```bash
sudo visudo -c -f deploy/bridge-sudoers   # проверить синтаксис перед установкой
sudo cp deploy/bridge-sudoers /etc/sudoers.d/bridge
sudo chmod 440 /etc/sudoers.d/bridge
sudo visudo -c                            # проверить, что всё корректно
```

Если `systemctl` на вашем сервере лежит не по пути `/usr/bin/systemctl`
(проверьте: `which systemctl`), поправьте путь в `deploy/bridge-sudoers` перед
установкой.

⚠️ Порт веб-интерфейса лучше не открывать напрямую в интернет — либо закрыть
файрволом кроме доверенных IP, либо пробросить через SSH-туннель
(`ssh -L 8765:localhost:8765 user@server`), либо поставить nginx с HTTPS перед ним.

## HTTP API для прямой пересылки в MAX (в обход Telegram-группы)

Telegram-боты не получают через `getUpdates` сообщения, отправленные **другими
ботами** в группе — это ограничение платформы, не обходится настройками
privacy mode. Если у вас есть свой сервис/бот, который тоже пишет в исходную
Telegram-группу (например, уведомления с сайта), мост их не увидит.

Решение — слать такие сообщения в MAX напрямую, минуя Telegram, через
встроенный HTTP-эндпоинт моста.

Добавьте в `.env`:
```
FORWARD_TOKEN=<длинный случайный токен, например: openssl rand -hex 32>
FORWARD_PORT=8766
```

Вызов из любого другого сервиса:
```bash
curl -X POST http://<ip-сервера-моста>:8766/forward \
  -H "Authorization: Bearer <FORWARD_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"text": "Текст сообщения"}'
```

Опционально можно переопределить чат-получатель в MAX на конкретный запрос:
```json
{"text": "...", "max_chat_id": 123456}
```
(если не передан — используется `MAX_TARGET_CHAT_ID` из `.env`).

Порт `FORWARD_PORT` нужно будет открыть в файрволе, если вызывающий сервис
находится на другом сервере — токен в `Authorization` защищает эндпоинт от
посторонних, но по возможности лучше тоже ограничить доступ по IP.

## Деплой на Linux VPS (systemd) — пошагово

Ниже всё выполняется с вашего Windows-компьютера через обычный SSH
(Git Bash / PowerShell / встроенный `ssh` в Windows 10+). Замените
`user@1.2.3.4` на реальные логин и IP вашего сервера.

### 1. Подключение по SSH

```bash
ssh user@1.2.3.4
```

Если раньше не подключались — примите отпечаток ключа сервера (`yes`) и
введите пароль (или используется ключ, если он уже настроен).

Если хотите зайти по SSH-ключу без пароля (рекомендуется), заранее с Windows:
```bash
ssh-keygen -t ed25519          # если ключа ещё нет (~/.ssh/id_ed25519)
ssh-copy-id user@1.2.3.4       # скопирует публичный ключ на сервер
```

Дальше все команды выполняются **уже внутри SSH-сессии на сервере**, если не
указано иное.

### 2. Системный пользователь и папка проекта

```bash
sudo useradd -r -m -d /opt/telegram-max-bridge -s /usr/sbin/nologin bridge
sudo mkdir -p /opt/telegram-max-bridge
sudo chown bridge:bridge /opt/telegram-max-bridge
```

### 3. Копирование файлов проекта на сервер

Самый простой способ — с вашего Windows-компьютера, **в отдельном окне
терминала** (не внутри SSH-сессии), выполните `scp` из папки проекта:

```bash
cd C:\Users\bikov\Projects\telegram-max-bridge
scp -r bridge deploy requirements.txt README.md .env.example user@1.2.3.4:/tmp/telegram-max-bridge
```

Затем обратно в SSH-сессии на сервере перенесите файлы в целевую папку и
поправьте владельца. Обратите внимание на `/.` в конце пути источника —
без него `cp -r ...*` пропустит скрытые файлы вроде `.env.example`:
```bash
sudo cp -r /tmp/telegram-max-bridge/. /opt/telegram-max-bridge/
sudo chown -R bridge:bridge /opt/telegram-max-bridge
rm -rf /tmp/telegram-max-bridge
```

(Если предпочитаете git — можно вместо scp сделать `git clone` репозитория
прямо в `/opt/telegram-max-bridge` из-под пользователя `bridge`.)

### 4. Виртуальное окружение и зависимости

```bash
sudo -u bridge python3 -m venv /opt/telegram-max-bridge/.venv
sudo -u bridge /opt/telegram-max-bridge/.venv/bin/pip install --upgrade pip
sudo -u bridge /opt/telegram-max-bridge/.venv/bin/pip install -r /opt/telegram-max-bridge/requirements.txt
```

Если `python3` окажется старой версии (нужен 3.10+), проверьте: `python3 --version`.

### 5. Конфигурация

```bash
sudo -u bridge cp /opt/telegram-max-bridge/.env.example /opt/telegram-max-bridge/.env
sudo -u bridge nano /opt/telegram-max-bridge/.env
```

Заполните как минимум: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SOURCE_CHAT_ID`,
`MAX_TARGET_CHAT_ID`, `WEBUI_USERNAME`/`WEBUI_PASSWORD` (для веб-интерфейса).
Сохраните: `Ctrl+O`, `Enter`, `Ctrl+X`.

### 6. Первый вход в MAX (интерактивный, разово)

Это единственный шаг, который нельзя автоматизировать заранее — нужно
отсканировать QR глазами:

```bash
sudo -u bridge /opt/telegram-max-bridge/.venv/bin/python -m bridge.list_max_chats_qr
```

В терминале появится ASCII QR-код — отсканируйте его в приложении MAX
(Профиль → Устройства → Подключить устройство). После подтверждения сессия
сохранится в `/opt/telegram-max-bridge/max_session/`, повторять не нужно.

### 7. Установка systemd-сервисов

```bash
sudo cp /opt/telegram-max-bridge/deploy/telegram-max-bridge.service /etc/systemd/system/
sudo cp /opt/telegram-max-bridge/deploy/telegram-max-bridge-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-max-bridge
sudo systemctl enable --now telegram-max-bridge-web
```

### 8. Проверка

```bash
sudo systemctl status telegram-max-bridge
sudo systemctl status telegram-max-bridge-web
sudo journalctl -u telegram-max-bridge -f
```

Должно быть `active (running)` у обоих, а в логе — строка
`соединение с MAX установлено`. Напишите сообщение в Telegram-группу и
проверьте, дошло ли оно в MAX.

### 9. Открыть веб-интерфейс безопасно

Порт `8765` наружу не открывайте. С Windows пробросьте туннель и откройте
локально в браузере:
```bash
ssh -L 8765:localhost:8765 user@1.2.3.4
```
Затем на компьютере откройте `http://localhost:8765`.

### Обновление после правок в .env через веб-интерфейс

```bash
sudo systemctl restart telegram-max-bridge
```

### Деплой обновлённого кода одной командой

Вместо ручного копирования файлов через `tee`/`scp` при каждом изменении кода
используйте `deploy/deploy.sh` — запускается с вашего компьютера (там, где
редактируете код), пакует `bridge/`, `requirements.txt`, `deploy/`, `README.md`
в архив, копирует на сервер, ставит зависимости и перезапускает сервисы.
`.env` и `max_session/` на сервере не трогаются.

```bash
./deploy/deploy.sh user@1.2.3.4
```

Если путь на сервере отличается от `/opt/telegram-max-bridge`, укажите вторым
аргументом:
```bash
./deploy/deploy.sh user@1.2.3.4 /opt/my-custom-path
```

### Полезные команды

```bash
sudo systemctl restart telegram-max-bridge telegram-max-bridge-web   # перезапуск
sudo systemctl stop telegram-max-bridge telegram-max-bridge-web      # остановка
sudo journalctl -u telegram-max-bridge -n 100 --no-pager              # последние 100 строк лога
```

### Резервная копия секретов

`.env` и `max_session/` на сервере — единственная копия токенов и сессии MAX,
в git не попадают (см. `.gitignore`). Рекомендуется периодически копировать их
куда-то отдельно от сервера (например, себе на компьютер):

```bash
scp root@<ip-сервера>:/opt/telegram-max-bridge/.env ./bridge-backup.env
scp root@<ip-сервера>:/opt/telegram-max-bridge/max_session/max_session.db ./bridge-backup-session.db
```

Храните эти файлы так же осторожно, как пароли — `.env` содержит токен бота и
`FORWARD_TOKEN`, а файл сессии даёт доступ к аккаунту MAX без пароля.

## Лицензия

[MIT](LICENSE) — используйте, изменяйте и распространяйте свободно, с
указанием авторства.
