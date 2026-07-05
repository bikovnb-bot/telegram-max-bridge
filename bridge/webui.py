"""Веб-интерфейс: вкладки с настройками, статусом, инструкцией и обслуживанием.

Запуск: python -m bridge.webui
Слушает на 0.0.0.0:WEBUI_PORT (по умолчанию 8765), защищён HTTP Basic Auth.
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from .config import Route, Settings, serialize_routes, update_env
from .status import read_status

app = FastAPI(title="telegram-max-bridge")
security = HTTPBasic()

SERVICE_UNITS = {
    "bridge": "telegram-max-bridge",
    "webui": "telegram-max-bridge-web",
}


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    settings = Settings.from_env()
    if not settings.webui_password:
        raise HTTPException(
            status_code=500,
            detail="WEBUI_PASSWORD не задан в .env — установите пароль перед запуском веб-интерфейса",
        )
    ok_user = secrets.compare_digest(credentials.username, settings.webui_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.webui_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Неверный логин/пароль",
            headers={"WWW-Authenticate": "Basic"},
        )


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


async def _call_bridge(
    settings: Settings, method: str, path: str, json_body: dict | None = None
) -> tuple[int, dict]:
    if not settings.forward_token:
        return 500, {"error": "FORWARD_TOKEN не настроен в .env"}

    url = f"http://127.0.0.1:{settings.forward_port}{path}"
    headers = {"Authorization": f"Bearer {settings.forward_token}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, json=json_body, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
                return resp.status, data
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"Не удалось обратиться к мосту: {exc}"}


async def _run_systemctl(action: str, unit: str) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", action, unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode == 0, out.decode(errors="replace")
    except FileNotFoundError as exc:
        return False, str(exc)


def render_page(settings: Settings, message: str | None = None) -> str:
    status = read_status(settings.status_file)
    log_tail = ""
    if os.path.exists(settings.log_file):
        with open(settings.log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
            log_tail = "".join(lines)

    connected = bool(status.get("max_connected"))
    connected_badge = (
        '<span class="badge badge-ok">🟢 подключено</span>'
        if connected
        else '<span class="badge badge-err">🔴 не подключено</span>'
    )
    recent_rows = "".join(
        f"<tr><td>{html.escape(str(r.get('max_chat_id')))}</td>"
        f"<td>{html.escape(r.get('text', ''))}</td></tr>"
        for r in status.get("recent", [])
    ) or "<tr><td colspan='2' class='muted'>Пока ничего не пересылалось</td></tr>"

    message_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""

    route_rows = "".join(
        f"""<tr>
          <td><input name="route_tg" value="{r.telegram_chat_id}"></td>
          <td><input name="route_max" value="{r.max_chat_id}"></td>
          <td><input name="route_label" value="{html.escape(r.label)}" placeholder="метка (необязательно)"></td>
          <td><button type="button" class="secondary" onclick="this.closest('tr').remove()">✕</button></td>
        </tr>"""
        for r in settings.routes
    )

    route_options = "".join(
        f'<option value="{r.max_chat_id}">{html.escape(r.label or str(r.max_chat_id))} ({r.max_chat_id})</option>'
        for r in settings.routes
    )

    routes_summary = (
        ", ".join(f"{r.telegram_chat_id}→{r.max_chat_id}" for r in settings.routes)
        or "маршруты не настроены"
    )

    return f"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>telegram-max-bridge</title>
<style>
:root {{
  --bg: #0f1115;
  --card: #171a21;
  --border: #262b36;
  --text: #e6e8eb;
  --muted: #8b93a3;
  --accent: #4f8cff;
  --ok: #2ecc71;
  --err: #ff5c5c;
  --warn: #f5a623;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  max-width: 1000px;
  margin: 0 auto;
  padding: 2rem 1.25rem 4rem;
}}
h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
h2 {{ font-size: 1.1rem; margin-top: 0; color: var(--text); }}
h3 {{ font-size: 1rem; margin: 1.1rem 0 0.3rem; }}
.subtitle {{ color: var(--muted); margin-top: 0; margin-bottom: 1.5rem; }}
.card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.25rem;
}}
.status-row {{ display: flex; gap: 1.5rem; align-items: center; flex-wrap: wrap; }}
.badge {{ padding: 0.25rem 0.7rem; border-radius: 999px; font-weight: 600; font-size: 0.9rem; }}
.badge-ok {{ background: rgba(46,204,113,0.15); color: var(--ok); }}
.badge-err {{ background: rgba(255,92,92,0.15); color: var(--err); }}
.badge-warn {{ background: rgba(245,166,35,0.15); color: var(--warn); }}
.stat {{ color: var(--muted); }}
.stat b {{ color: var(--text); }}
label {{ display: block; margin-top: 0.9rem; margin-bottom: 0.3rem; font-weight: 600; font-size: 0.9rem; color: var(--muted); }}
input, select {{
  width: 100%; padding: 0.55rem 0.7rem; font-size: 0.95rem;
  background: #0d0f13; border: 1px solid var(--border); border-radius: 6px; color: var(--text);
}}
input:focus, select:focus {{ outline: none; border-color: var(--accent); }}
button {{
  margin-top: 1rem; padding: 0.55rem 1.2rem; font-size: 0.95rem;
  background: var(--accent); color: white; border: none; border-radius: 6px; cursor: pointer;
  font-weight: 600;
}}
button:hover {{ opacity: 0.9; }}
button.secondary {{ background: #2a2f3a; }}
button.danger {{ background: #6b2b2b; }}
pre {{
  background: #0a0b0e; color: #c7d0da; padding: 1rem; max-height: 320px; overflow: auto;
  border-radius: 8px; font-size: 0.82rem; border: 1px solid var(--border);
}}
table {{ border-collapse: collapse; width: 100%; margin-top: 0.5rem; font-size: 0.9rem; }}
td, th {{ border-bottom: 1px solid var(--border); padding: 6px 8px; text-align: left; }}
th {{ color: var(--muted); font-weight: 600; }}
.muted {{ color: var(--muted); }}
.msg {{ background: rgba(46,204,113,0.12); color: var(--ok); padding: 0.6rem 1rem; border-radius: 6px; margin-bottom: 1rem; }}
.hint {{ font-size: 0.85rem; color: var(--muted); margin-top: 0.5rem; }}
code {{ background: #0d0f13; padding: 0.1rem 0.4rem; border-radius: 4px; }}
ol, ul {{ padding-left: 1.2rem; }}
ol li, ul li {{ margin-bottom: 0.5rem; }}
#chatsResult, #sendResult, #restartResult {{ margin-top: 0.75rem; font-size: 0.9rem; }}

.tabs {{ display: flex; gap: 0.25rem; margin-bottom: 1.25rem; border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
.tab-btn {{
  margin-top: 0; padding: 0.6rem 1.1rem; background: transparent; color: var(--muted);
  border: none; border-bottom: 2px solid transparent; border-radius: 0; font-weight: 600; cursor: pointer;
}}
.tab-btn.active {{ color: var(--text); border-bottom-color: var(--accent); }}
.tab-btn:hover {{ opacity: 1; color: var(--text); }}
.tab-panel {{ display: none; }}
.tab-panel.active {{ display: block; }}
.service-row {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.6rem 0; border-bottom: 1px solid var(--border); }}
.service-row:last-child {{ border-bottom: none; }}
</style>
</head>
<body>
<h1>telegram-max-bridge</h1>
<p class="subtitle">Статус моста, настройка получателей, инструкция и обслуживание</p>
{message_html}

<div class="card">
  <div class="status-row">
    <div>MAX: {connected_badge}</div>
    <div class="stat">Переслано сообщений: <b>{status.get('forwarded_count', 0)}</b></div>
    <div class="stat">Маршруты: <b>{html.escape(routes_summary)}</b></div>
    {"<div class='stat' style='color:var(--err)'>Ошибка: " + html.escape(status.get('last_error') or '') + "</div>" if status.get('last_error') else ''}
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab('overview', this)">Обзор</button>
  <button class="tab-btn" onclick="showTab('routes', this)">Маршруты</button>
  <button class="tab-btn" onclick="showTab('settings', this)">Настройки</button>
  <button class="tab-btn" onclick="showTab('maintenance', this)">Обслуживание</button>
  <button class="tab-btn" onclick="showTab('guide', this)">Инструкция</button>
</div>

<div id="tab-overview" class="tab-panel active">
  <div class="card">
    <h2>Последние пересланные сообщения</h2>
    <table>
    <tr><th>MAX chat_id</th><th>текст</th></tr>
    {recent_rows}
    </table>
  </div>
  <div class="card">
    <h2>Лог (последние строки)</h2>
    <pre>{html.escape(log_tail)}</pre>
  </div>
</div>

<div id="tab-routes" class="tab-panel">
  <div class="card">
    <h2>Маршруты пересылки (Telegram-чат → MAX-чат)</h2>
    <form method="post" action="/save" id="routesForm">
      <table id="routesTable">
        <tr><th>Telegram Chat ID</th><th>MAX Chat ID</th><th>метка</th><th></th></tr>
        {route_rows}
      </table>
      <button type="button" class="secondary" onclick="addRoute()">+ Добавить маршрут</button>
      <div style="margin-top:1.2rem;">
        <button type="submit">Сохранить маршруты</button>
      </div>
    </form>
    <p class="hint">После сохранения перезапустите мост (вкладка "Обслуживание"), чтобы изменения применились.</p>
  </div>

  <div class="card">
    <h2>Чаты MAX</h2>
    <p class="hint" style="margin-top:0;">Аккаунт MAX, залогиненный в мосте, должен уже состоять в нужном чате/группе.</p>
    <button type="button" class="secondary" onclick="loadChats()">Показать чаты MAX</button>
    <div id="chatsResult"></div>
  </div>

  <div class="card">
    <h2>Проверка пересылки</h2>
    <label>Куда отправить</label>
    <select id="testTarget">{route_options}</select>
    <label>Текст тестового сообщения</label>
    <input id="testText" value="Тестовое сообщение из веб-интерфейса моста">
    <button type="button" onclick="sendTest()">Отправить тестовое сообщение в MAX</button>
    <div id="sendResult"></div>
  </div>
</div>

<div id="tab-settings" class="tab-panel">
  <div class="card">
    <h2>Настройки</h2>
    <form method="post" action="/save" id="settingsForm">
      <label>TELEGRAM_BOT_TOKEN</label>
      <input name="TELEGRAM_BOT_TOKEN" value="{html.escape(_mask(settings.telegram_bot_token))}"
             placeholder="оставьте как есть, если не меняете">

      <label>ALERT_CHAT_ID (личка в Telegram, куда слать алерты о сбоях, необязательно)</label>
      <input name="ALERT_CHAT_ID" value="{settings.alert_chat_id or ''}">

      <label>ALERT_DISCONNECT_SECONDS (через сколько секунд простоя MAX слать алерт)</label>
      <input name="ALERT_DISCONNECT_SECONDS" value="{settings.alert_disconnect_seconds}">

      <label>Антифлуд: максимум сообщений...</label>
      <input name="RATE_LIMIT_MAX" value="{settings.rate_limit_max}" placeholder="напр. 20">
      <label>...за сколько секунд (на один чат-источник, лишнее отбрасывается)</label>
      <input name="RATE_LIMIT_WINDOW_SECONDS" value="{settings.rate_limit_window_seconds}" placeholder="напр. 60">

      <label>Пересылать ответы из MAX обратно в Telegram</label>
      <select name="REVERSE_FORWARD_ENABLED">
        <option value="true" {"selected" if settings.reverse_forward_enabled else ""}>Включено</option>
        <option value="false" {"selected" if not settings.reverse_forward_enabled else ""}>Выключено</option>
      </select>

      <label>Мониторинг хоста: порог диска, % (алерт при превышении)</label>
      <input name="DISK_ALERT_PERCENT" value="{settings.disk_alert_percent:g}" placeholder="напр. 90">
      <label>Мониторинг хоста: порог памяти, %</label>
      <input name="MEM_ALERT_PERCENT" value="{settings.mem_alert_percent:g}" placeholder="напр. 90">

      <div style="margin-top:1.2rem;">
        <button type="submit">Сохранить настройки</button>
      </div>
    </form>
    <p class="hint">После сохранения перезапустите мост (вкладка "Обслуживание"), чтобы изменения применились.</p>
  </div>
</div>

<div id="tab-maintenance" class="tab-panel">
  <div class="card">
    <h2>Сервисы</h2>
    <p class="hint" style="margin-top:0;">Работает только на сервере (systemd) — локально при разработке кнопки вернут ошибку, это нормально.</p>
    <div id="serviceStatus">
      <div class="service-row">
        <div>Мост (telegram-max-bridge) — <span id="status-bridge" class="muted">проверка...</span></div>
        <button type="button" class="secondary" onclick="restartService('bridge')">Перезапустить</button>
      </div>
      <div class="service-row">
        <div>Веб-интерфейс (telegram-max-bridge-web) — <span id="status-webui" class="muted">проверка...</span></div>
        <button type="button" class="secondary" onclick="restartService('webui')">Перезапустить</button>
      </div>
    </div>
    <button type="button" class="secondary" onclick="loadServiceStatus()">Обновить статус</button>
    <div id="restartResult"></div>
    <p class="hint">Для работы кнопок нужны права sudo для пользователя, от которого запущен веб-интерфейс —
    см. раздел про <code>deploy/bridge-sudoers</code> в README.</p>
  </div>
</div>

<div id="tab-guide" class="tab-panel">
  <div class="card">
    <h2>Как всё это работает</h2>
    <p>Мост слушает сообщения в Telegram-группах (через обычного Telegram-бота) и пересылает их в
    указанные чаты/группы MAX (через личный аккаунт MAX, залогиненный по QR-коду). Также есть
    HTTP API для прямой пересылки в MAX в обход Telegram — полезно, если другой сервис (например,
    ваш сайт) шлёт уведомления в ту же Telegram-группу через своего бота: Telegram не показывает
    ботам сообщения других ботов, поэтому такие уведомления нужно слать напрямую через API.</p>
  </div>

  <div class="card">
    <h2>Шаг 1. Создать Telegram-бота</h2>
    <ol>
      <li>Откройте @BotFather в Telegram, отправьте <code>/newbot</code>, следуйте инструкциям</li>
      <li>Скопируйте выданный токен вида <code>123456:AAAbbbCCCddd...</code></li>
      <li>Впишите его во вкладке "Настройки" в поле <code>TELEGRAM_BOT_TOKEN</code></li>
    </ol>
  </div>

  <div class="card">
    <h2>Шаг 2. Добавить бота в нужную Telegram-группу</h2>
    <ol>
      <li>Откройте группу в Telegram → участники → "Добавить участников" → найдите вашего бота</li>
      <li>Выключите ему Privacy Mode: @BotFather → <code>/mybots</code> → выбрать бота →
        Bot Settings → Group Privacy → Turn off (иначе бот не увидит обычные сообщения)</li>
      <li><b>Важно:</b> если бот уже был в группе до выключения Privacy Mode, настройка не применится
        задним числом — удалите бота из группы и добавьте заново</li>
    </ol>
  </div>

  <div class="card">
    <h2>Шаг 3. Узнать Telegram Chat ID</h2>
    <ol>
      <li>Напишите любое сообщение в группу (после шага 2)</li>
      <li>Откройте в браузере: <code>https://api.telegram.org/bot&lt;ТОКЕН&gt;/getUpdates</code></li>
      <li>Найдите в ответе <code>"chat":{{"id": ...}}</code> — для групп обычно отрицательное число</li>
      <li>Если ответ пустой (<code>"result":[]</code>) — напишите новое сообщение в группу и запросите снова
        (getUpdates показывает только новые сообщения)</li>
    </ol>
  </div>

  <div class="card">
    <h2>Шаг 4. Узнать MAX Chat ID и создать маршрут</h2>
    <ol>
      <li>Аккаунт MAX, под которым залогинен мост, должен состоять в нужном чате/группе MAX</li>
      <li>Во вкладке "Маршруты" нажмите "Показать чаты MAX" — появится список с id и названиями</li>
      <li>Нажмите "Выбрать" у нужного чата — добавится строка в таблицу маршрутов</li>
      <li>Впишите в ту же строку Telegram Chat ID из шага 3</li>
      <li>Нажмите "Сохранить маршруты", затем перезапустите мост на вкладке "Обслуживание"</li>
    </ol>
    <p class="hint">Можно настроить несколько маршрутов одновременно — разные группы Telegram
    в разные чаты MAX. Фото и документы пересылаются вместе с текстом; служебные сообщения
    (участник добавлен/вышел и т.п.) не пересылаются.</p>
  </div>

  <div class="card">
    <h2>Шаг 5. Проверить</h2>
    <p>Во вкладке "Маршруты" внизу — "Отправить тестовое сообщение в MAX", либо напишите
    реальное сообщение в Telegram-группу и проверьте, пришло ли оно в MAX.</p>
  </div>

  <div class="card">
    <h2>Дополнительно: прямая пересылка через HTTP (для других сервисов)</h2>
    <p>Если у вас есть сайт/сервис, который тоже шлёт уведомления в ту же Telegram-группу через
    своего бота — Telegram не даст мосту увидеть эти сообщения. Решение — слать их в MAX
    напрямую, минуя Telegram:</p>
    <pre>curl -X POST http://&lt;ip-сервера&gt;:&lt;FORWARD_PORT&gt;/forward \\
  -H "Authorization: Bearer &lt;FORWARD_TOKEN&gt;" \\
  -H "Content-Type: application/json" \\
  -d '{{"text": "Текст сообщения"}}'</pre>
    <p class="hint">Опционально можно передать <code>max_chat_id</code> в теле запроса, чтобы
    переопределить получателя — иначе используется первый маршрут из списка.
    <code>FORWARD_TOKEN</code> и <code>FORWARD_PORT</code> — в файле <code>.env</code> на сервере.</p>
  </div>

  <div class="card">
    <h2>Частые проблемы</h2>
    <ul>
      <li><b>Сообщение не приходит в MAX, хотя бот видит его в Telegram</b> — проверьте, что для
      этой Telegram-группы настроен маршрут (вкладка "Маршруты"), и что MAX-аккаунт состоит в
      целевом чате MAX</li>
      <li><b>Пусто в getUpdates</b> — отправьте новое сообщение в группу после последних изменений
      настроек privacy, запросите снова</li>
      <li><b>Уведомления от другого бота (не Telegram-бота этого моста) не долетают</b> — это
      ограничение Telegram (боты не видят сообщения других ботов), используйте HTTP API выше</li>
      <li><b>MAX пишет "Bot has restriction to input"</b> — сессия MAX не может писать в личку
      самой себе ("Избранное"), выберите обычный чат/группу</li>
    </ul>
  </div>
</div>

<script>
function showTab(name, btn) {{
  document.querySelectorAll('.tab-panel').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'maintenance') {{ loadServiceStatus(); }}
}}

async function loadChats() {{
  const el = document.getElementById('chatsResult');
  el.innerHTML = '<p class="muted">Загрузка...</p>';
  try {{
    const r = await fetch('/api/chats');
    const data = await r.json();
    if (!r.ok) {{
      el.innerHTML = '<p style="color:var(--err)">Ошибка: ' + (data.error || r.status) + '</p>';
      return;
    }}
    if (!data.chats || data.chats.length === 0) {{
      el.innerHTML = '<p class="muted">Чатов не найдено</p>';
      return;
    }}
    let rows = data.chats.map(c =>
      `<tr><td>${{c.id}}</td><td>${{c.type}}</td><td>${{c.title || '(без названия)'}}</td>` +
      `<td><button type="button" class="secondary" onclick="addRoute(null, ${{c.id}}, ${{JSON.stringify(c.title || '')}})">Выбрать</button></td></tr>`
    ).join('');
    el.innerHTML = '<table><tr><th>id</th><th>тип</th><th>название</th><th></th></tr>' + rows + '</table>';
  }} catch (e) {{
    el.innerHTML = '<p style="color:var(--err)">Ошибка запроса: ' + e + '</p>';
  }}
}}

function addRoute(tgId, maxId, label) {{
  const table = document.getElementById('routesTable');
  const tr = document.createElement('tr');
  tr.innerHTML =
    `<td><input name="route_tg" value="${{tgId ?? ''}}"></td>` +
    `<td><input name="route_max" value="${{maxId ?? ''}}"></td>` +
    `<td><input name="route_label" value="${{label ?? ''}}" placeholder="метка (необязательно)"></td>` +
    `<td><button type="button" class="secondary" onclick="this.closest('tr').remove()">✕</button></td>`;
  table.appendChild(tr);
}}

async function sendTest() {{
  const el = document.getElementById('sendResult');
  const text = document.getElementById('testText').value;
  const maxChatId = document.getElementById('testTarget').value;
  el.innerHTML = '<p class="muted">Отправка...</p>';
  try {{
    const r = await fetch('/api/test-send', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text, max_chat_id: maxChatId ? parseInt(maxChatId, 10) : null }})
    }});
    const data = await r.json();
    if (!r.ok) {{
      el.innerHTML = '<p style="color:var(--err)">Ошибка: ' + (data.error || r.status) + '</p>';
      return;
    }}
    el.innerHTML = '<p style="color:var(--ok)">Отправлено успешно</p>';
  }} catch (e) {{
    el.innerHTML = '<p style="color:var(--err)">Ошибка запроса: ' + e + '</p>';
  }}
}}

async function loadServiceStatus() {{
  try {{
    const r = await fetch('/api/service-status');
    const data = await r.json();
    for (const key of ['bridge', 'webui']) {{
      const el = document.getElementById('status-' + key);
      const val = data[key] || 'unknown';
      el.textContent = val;
      el.className = val === 'active' ? '' : 'muted';
      el.style.color = val === 'active' ? 'var(--ok)' : 'var(--err)';
    }}
  }} catch (e) {{
    // тихо игнорируем — например, локально без sudo/systemctl
  }}
}}

async function restartService(key) {{
  const el = document.getElementById('restartResult');
  el.innerHTML = '<p class="muted">Перезапуск...</p>';
  try {{
    const r = await fetch('/api/restart/' + key, {{ method: 'POST' }});
    const data = await r.json();
    if (!r.ok) {{
      el.innerHTML = '<p style="color:var(--err)">Ошибка: ' + (data.error || r.status) + '</p>';
      return;
    }}
    el.innerHTML = '<p style="color:var(--ok)">' + (data.note || 'Перезапущено') + '</p>';
    setTimeout(loadServiceStatus, 3000);
  }} catch (e) {{
    el.innerHTML = '<p style="color:var(--err)">Ошибка запроса: ' + e + '</p>';
  }}
}}

document.addEventListener('DOMContentLoaded', loadServiceStatus);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index(_: None = Depends(check_auth)) -> str:
    settings = Settings.from_env()
    return render_page(settings)


@app.post("/save", response_class=HTMLResponse)
async def save(request: Request, _: None = Depends(check_auth)) -> RedirectResponse:
    form = await request.form()

    values: dict[str, str] = {}

    token = str(form.get("TELEGRAM_BOT_TOKEN", "")).strip()
    # Если поле не тронуто, там останется маска со звёздочками — токен не перезаписываем.
    if token and "*" not in token:
        values["TELEGRAM_BOT_TOKEN"] = token

    if "ALERT_CHAT_ID" in form:
        values["ALERT_CHAT_ID"] = str(form.get("ALERT_CHAT_ID", "")).strip()

    alert_seconds = str(form.get("ALERT_DISCONNECT_SECONDS", "")).strip()
    if alert_seconds:
        values["ALERT_DISCONNECT_SECONDS"] = alert_seconds

    rate_max = str(form.get("RATE_LIMIT_MAX", "")).strip()
    if rate_max:
        values["RATE_LIMIT_MAX"] = rate_max

    rate_window = str(form.get("RATE_LIMIT_WINDOW_SECONDS", "")).strip()
    if rate_window:
        values["RATE_LIMIT_WINDOW_SECONDS"] = rate_window

    if "REVERSE_FORWARD_ENABLED" in form:
        values["REVERSE_FORWARD_ENABLED"] = str(form.get("REVERSE_FORWARD_ENABLED", "true")).strip()

    disk_pct = str(form.get("DISK_ALERT_PERCENT", "")).strip()
    if disk_pct:
        values["DISK_ALERT_PERCENT"] = disk_pct

    mem_pct = str(form.get("MEM_ALERT_PERCENT", "")).strip()
    if mem_pct:
        values["MEM_ALERT_PERCENT"] = mem_pct

    if "route_tg" in form:
        tg_ids = form.getlist("route_tg")
        max_ids = form.getlist("route_max")
        labels = form.getlist("route_label")
        routes: list[Route] = []
        for i in range(len(tg_ids)):
            tg_raw = str(tg_ids[i]).strip()
            max_raw = str(max_ids[i]).strip() if i < len(max_ids) else ""
            label = str(labels[i]).strip() if i < len(labels) else ""
            if not tg_raw or not max_raw:
                continue
            try:
                routes.append(Route(int(tg_raw), int(max_raw), label))
            except ValueError:
                continue

        values["ROUTES"] = serialize_routes(routes)
        # Отключаем старые одиночные переменные, чтобы не путали при следующем чтении.
        values["TELEGRAM_SOURCE_CHAT_ID"] = ""
        values["MAX_TARGET_CHAT_ID"] = ""

    update_env(values)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/chats")
async def api_chats(_: None = Depends(check_auth)) -> JSONResponse:
    settings = Settings.from_env()
    status_code, data = await _call_bridge(settings, "GET", "/chats")
    return JSONResponse(data, status_code=status_code)


@app.post("/api/test-send")
async def api_test_send(request: Request, _: None = Depends(check_auth)) -> JSONResponse:
    settings = Settings.from_env()
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    payload: dict = {"text": text}
    max_chat_id = body.get("max_chat_id")
    if max_chat_id:
        payload["max_chat_id"] = max_chat_id

    status_code, data = await _call_bridge(settings, "POST", "/forward", payload)
    return JSONResponse(data, status_code=status_code)


@app.get("/api/service-status")
async def api_service_status(_: None = Depends(check_auth)) -> JSONResponse:
    result: dict[str, str] = {}
    for key, unit in SERVICE_UNITS.items():
        ok, out = await _run_systemctl("is-active", unit)
        result[key] = out.strip() if out.strip() else ("active" if ok else "unknown")
    return JSONResponse(result)


@app.post("/api/restart/{key}")
async def api_restart(key: str, _: None = Depends(check_auth)) -> JSONResponse:
    if key not in SERVICE_UNITS:
        return JSONResponse({"error": "unknown service"}, status_code=404)
    unit = SERVICE_UNITS[key]

    if key == "webui":
        # Сам процесс скоро умрёт вместе с рестартом — отвечаем сразу,
        # а перезапуск планируем с небольшой задержкой.
        async def delayed_restart() -> None:
            await asyncio.sleep(1)
            await _run_systemctl("restart", unit)

        asyncio.create_task(delayed_restart())
        return JSONResponse(
            {"ok": True, "note": "Перезапуск веб-интерфейса запущен — страница станет недоступна на пару секунд"}
        )

    ok, out = await _run_systemctl("restart", unit)
    if not ok:
        return JSONResponse({"error": out[-2000:] or "не удалось перезапустить"}, status_code=500)
    return JSONResponse({"ok": True})


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(app, host="0.0.0.0", port=settings.webui_port)


if __name__ == "__main__":
    run()
