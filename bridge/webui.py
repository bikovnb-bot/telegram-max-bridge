"""Веб-интерфейс: вкладки с настройками, статусом, инструкцией и обслуживанием.

Запуск: python -m bridge.webui
Слушает на 0.0.0.0:WEBUI_PORT (по умолчанию 8765), защищён HTTP Basic Auth.
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import time
from urllib.parse import urlsplit

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from .config import Route, Settings, clean_label, serialize_routes, update_env
from .status import read_status

app = FastAPI(title="telegram-max-bridge")
security = HTTPBasic()

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_same_origin_request(method: str, headers) -> bool:
    """Защита от CSRF: запрос, меняющий состояние, должен идти с нашего же origin.

    Basic Auth браузер подставляет сам, в том числе в запросы с чужой страницы,
    поэтому сверяем Sec-Fetch-Site / Origin. Запросы без этих заголовков
    (curl, скрипты) пропускаем — их не инициирует браузер администратора.
    """
    if method.upper() in SAFE_METHODS:
        return True
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is not None:
        if origin == "null":
            return False
        return urlsplit(origin).netloc == headers.get("host", "")
    return True


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if not is_same_origin_request(request.method, request.headers):
        return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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


def _is_int(raw: str) -> bool:
    try:
        int(raw)
    except ValueError:
        return False
    return True


def _is_percent(raw: str) -> bool:
    try:
        return 0 < float(raw) <= 100
    except ValueError:
        return False


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
        '<span class="badge badge-ok"><span class="pulse-dot"></span> MAX Онлайн</span>'
        if connected
        else '<span class="badge badge-err"><span class="static-dot"></span> MAX Отключен</span>'
    )

    recent_items = status.get("recent", [])
    recent_rows_list = []
    for r in recent_items:
        ts = r.get("time")
        t_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"
        direction = r.get("direction", "tg->max")
        if direction == "max->tg":
            dir_badge = '<span class="pill pill-purple">MAX → TG</span>'
        elif direction == "http":
            dir_badge = '<span class="pill pill-green">HTTP API</span>'
        else:
            dir_badge = '<span class="pill pill-blue">TG → MAX</span>'

        chat_id_str = html.escape(str(r.get("max_chat_id", "")))
        text_str = html.escape(r.get("text", ""))
        recent_rows_list.append(
            f'<tr><td class="col-time font-mono">{t_str}</td><td>{dir_badge}</td><td class="font-mono text-purple">{chat_id_str}</td><td class="col-text">{text_str}</td></tr>'
        )
    recent_rows = "".join(recent_rows_list) or "<tr><td colspan='4' class='muted text-center py-4'>Пока ничего не пересылалось</td></tr>"

    message_html = f"<div class='toast-banner'>{html.escape(message)}</div>" if message else ""

    route_rows = "".join(
        f"""<tr>
          <td><input name="route_tg" value="{r.telegram_chat_id}" class="form-input font-mono" placeholder="ID группы TG"></td>
          <td><input name="route_max" value="{r.max_chat_id}" class="form-input font-mono" placeholder="ID чата MAX"></td>
          <td><input name="route_label" value="{html.escape(r.label)}" class="form-input" placeholder="метка (необязательно)"></td>
          <td class="text-right"><button type="button" class="btn-icon-danger" onclick="this.closest('tr').remove()" title="Удалить">✕</button></td>
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

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>telegram-max-bridge Control Panel</title>
<style>
:root {{
  --bg: #090d16;
  --sidebar: #0f1422;
  --card: #131929;
  --card-hover: #172033;
  --border: #1e283d;
  --border-focus: #3b82f6;
  --text: #f1f5f9;
  --muted: #8b99ad;
  --accent: #3b82f6;
  --accent-hover: #2563eb;
  --ok: #10b981;
  --err: #ef4444;
  --warn: #f59e0b;
  --purple: #a855f7;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}
.font-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
.topbar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.85rem 1.5rem;
  background: rgba(15, 20, 34, 0.85);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 40;
}}
.brand {{ display: flex; align-items: center; gap: 0.75rem; }}
.brand-icon {{
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #6366f1);
  display: flex; align-items: center; justify-content: center;
  font-size: 1.1rem; font-weight: bold; color: #fff;
  box-shadow: 0 4px 12px rgba(37,99,235,0.3);
}}
.brand-title {{ font-size: 1.05rem; font-weight: 700; letter-spacing: -0.02em; display: flex; align-items: center; gap: 0.5rem; }}
.brand-sub {{ font-size: 0.75rem; color: var(--muted); }}
.badge-version {{
  font-size: 0.7rem; font-weight: 600; padding: 0.15rem 0.45rem; border-radius: 999px;
  background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3);
}}
.top-status {{ display: flex; align-items: center; gap: 0.75rem; font-size: 0.8rem; }}
.layout {{ display: flex; flex: 1; max-width: 1300px; width: 100%; margin: 0 auto; padding: 1.25rem; gap: 1.5rem; }}
.sidebar {{ width: 220px; flex-shrink: 0; display: flex; flex-direction: column; gap: 0.35rem; }}
.sidebar-section {{ font-size: 0.7rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; padding: 0.5rem 0.75rem 0.2rem; }}
.tab-btn {{
  display: flex; align-items: center; gap: 0.65rem; width: 100%;
  padding: 0.65rem 0.85rem; border-radius: 10px;
  background: transparent; color: var(--muted); border: 1px solid transparent;
  font-size: 0.85rem; font-weight: 600; text-align: left; cursor: pointer; transition: all 0.15s;
}}
.tab-btn:hover {{ color: var(--text); background: var(--card); }}
.tab-btn.active {{ color: #93c5fd; background: rgba(59, 130, 246, 0.12); border-color: rgba(59, 130, 246, 0.3); }}
.tab-btn svg {{ width: 16px; height: 16px; stroke-width: 2; flex-shrink: 0; }}
.main-content {{ flex: 1; min-width: 0; }}
.tab-panel {{ display: none; flex-direction: column; gap: 1.25rem; }}
.tab-panel.active {{ display: flex; }}
.card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1.25rem 1.5rem;
  box-shadow: 0 4px 20px rgba(0,0,0,0.15);
}}
.card-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; }}
.card-title {{ font-size: 1.05rem; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }}
.card-desc {{ font-size: 0.8rem; color: var(--muted); margin-top: 0.2rem; }}
.metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.25rem; }}
.metric-box {{
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 1rem 1.2rem; position: relative; overflow: hidden;
}}
.metric-label {{ font-size: 0.75rem; font-weight: 600; color: var(--muted); margin-bottom: 0.35rem; }}
.metric-val {{ font-size: 1.5rem; font-weight: 800; color: #fff; letter-spacing: -0.02em; }}
.metric-sub {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.35rem; }}
.pulse-dot {{
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--ok); box-shadow: 0 0 0 rgba(16,185,129,0.7);
  animation: pulse-ring 2s infinite cubic-bezier(0.66, 0, 0, 1);
}}
@keyframes pulse-ring {{
  0% {{ box-shadow: 0 0 0 0 rgba(16,185,129,0.7); }}
  70% {{ box-shadow: 0 0 0 8px rgba(16,185,129,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(16,185,129,0); }}
}}
.static-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--err); }}
.badge {{
  display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.25rem 0.65rem;
  border-radius: 999px; font-size: 0.75rem; font-weight: 600;
}}
.badge-ok {{ background: rgba(16,185,129,0.12); color: var(--ok); border: 1px solid rgba(16,185,129,0.25); }}
.badge-err {{ background: rgba(239,68,68,0.12); color: var(--err); border: 1px solid rgba(239,68,68,0.25); }}
.pill {{
  display: inline-block; padding: 0.15rem 0.5rem; border-radius: 6px;
  font-size: 0.7rem; font-weight: 700;
}}
.pill-blue {{ background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3); }}
.pill-purple {{ background: rgba(168,85,247,0.15); color: #c084fc; border: 1px solid rgba(168,85,247,0.3); }}
.pill-green {{ background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }}
.table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.82rem; }}
th {{ background: #0c111e; color: var(--muted); font-weight: 600; padding: 0.65rem 0.9rem; text-align: left; border-bottom: 1px solid var(--border); }}
td {{ padding: 0.65rem 0.9rem; border-bottom: 1px solid var(--border); vertical-align: middle; }}
tr:last-child td {{ border-bottom: none; }}
tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
.col-time {{ width: 85px; color: var(--muted); }}
.col-text {{ max-width: 450px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.text-purple {{ color: #c084fc; }}
.text-right {{ text-align: right; }}
.text-center {{ text-align: center; }}
.muted {{ color: var(--muted); }}
.form-input, .form-select {{
  width: 100%; padding: 0.55rem 0.75rem; font-size: 0.85rem;
  background: #090d16; border: 1px solid var(--border); border-radius: 8px; color: var(--text);
  outline: none; transition: border-color 0.15s;
}}
.form-input:focus, .form-select:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(59,130,246,0.2); }}
.form-group {{ margin-bottom: 1rem; }}
.form-group label {{ display: block; font-size: 0.78rem; font-weight: 600; color: var(--muted); margin-bottom: 0.35rem; }}
.form-hint {{ font-size: 0.72rem; color: #64748b; margin-top: 0.3rem; }}
.btn {{
  display: inline-flex; align-items: center; justify-content: center; gap: 0.45rem;
  padding: 0.55rem 1.1rem; border-radius: 8px; font-size: 0.82rem; font-weight: 600;
  border: none; cursor: pointer; transition: all 0.15s;
}}
.btn-primary {{ background: var(--accent); color: #fff; box-shadow: 0 4px 12px rgba(59,130,246,0.25); }}
.btn-primary:hover {{ background: var(--accent-hover); }}
.btn-secondary {{ background: #1c2438; color: var(--text); border: 1px solid var(--border); }}
.btn-secondary:hover {{ background: #26314a; }}
.btn-icon-danger {{
  width: 28px; height: 28px; border-radius: 6px; border: none;
  background: transparent; color: #f87171; font-weight: bold; cursor: pointer;
}}
.btn-icon-danger:hover {{ background: rgba(239,68,68,0.2); color: #ef4444; }}
.terminal {{
  background: #060910; border: 1px solid var(--border); border-radius: 10px;
  padding: 1rem; font-family: ui-monospace, monospace; font-size: 0.75rem;
  line-height: 1.5; color: #cbd5e1; max-height: 380px; overflow-y: auto; white-space: pre-wrap;
}}
.service-item {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.9rem 1.1rem; background: #0c111e; border: 1px solid var(--border); border-radius: 10px; margin-bottom: 0.75rem;
}}
.service-title {{ font-size: 0.88rem; font-weight: 600; color: #fff; }}
.service-sub {{ font-size: 0.75rem; color: var(--muted); }}
.toast-banner {{
  background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.3); color: var(--ok);
  padding: 0.75rem 1.25rem; border-radius: 10px; font-size: 0.85rem; margin-bottom: 1.25rem;
}}
.modal-overlay {{
  position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
  z-index: 50; display: none; align-items: center; justify-content: center; padding: 1rem;
}}
.modal-overlay.active {{ display: flex; }}
.modal-card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  width: 100%; max-width: 550px; padding: 1.5rem; box-shadow: 0 10px 40px rgba(0,0,0,0.5);
}}
.modal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; padding-bottom: 0.75rem; border-bottom: 1px solid var(--border); }}
.modal-title {{ font-size: 1.05rem; font-weight: 700; color: #fff; }}
.modal-close {{ background: transparent; border: none; font-size: 1.2rem; color: var(--muted); cursor: pointer; }}
.modal-close:hover {{ color: #fff; }}
.toast-notification {{
  position: fixed; bottom: 1.5rem; right: 1.5rem;
  background: #0f172a; border: 1px solid #3b82f6; color: #fff;
  padding: 0.75rem 1.25rem; border-radius: 10px; font-size: 0.82rem; font-weight: 500;
  box-shadow: 0 10px 25px rgba(0,0,0,0.4); z-index: 60;
  transform: translateY(100px); opacity: 0; transition: all 0.25s; pointer-events: none;
}}
.toast-notification.show {{ transform: translateY(0); opacity: 1; }}
@media (max-width: 860px) {{
  .layout {{ flex-direction: column; }}
  .sidebar {{ width: 100%; flex-direction: row; overflow-x: auto; padding-bottom: 0.5rem; }}
  .sidebar-section {{ display: none; }}
  .tab-btn {{ white-space: nowrap; }}
}}
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <div class="brand-icon">⇄</div>
    <div>
      <div class="brand-title">
        <span>MAX Bridge</span>
        <span class="badge-version">v1.2</span>
      </div>
      <div class="brand-sub">Telegram ↔ MAX Gateway</div>
    </div>
  </div>

  <div class="top-status">
    <div>{connected_badge}</div>
    <button type="button" class="btn btn-secondary" onclick="restartService('bridge')" style="padding:0.4rem 0.8rem; font-size:0.75rem;">
      ↻ Перезапуск моста
    </button>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-section">Мониторинг</div>
    <button type="button" class="tab-btn active" onclick="showTab('overview', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
      <span>Дашборд</span>
    </button>
    <button type="button" class="tab-btn" onclick="showTab('routes', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M13 5l7 7-7 7M5 5l7 7-7 7"></path></svg>
      <span>Маршруты</span>
    </button>
    <button type="button" class="tab-btn" onclick="showTab('simulator', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
      <span>Тест отправки</span>
    </button>
    <button type="button" class="tab-btn" onclick="showTab('logs', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
      <span>Журнал (Логи)</span>
    </button>

    <div class="sidebar-section">Система</div>
    <button type="button" class="tab-btn" onclick="showTab('settings', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><circle cx="12" cy="12" r="3"></circle></svg>
      <span>Настройки .env</span>
    </button>
    <button type="button" class="tab-btn" onclick="showTab('maintenance', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"></path></svg>
      <span>Службы systemd</span>
    </button>
    <button type="button" class="tab-btn" onclick="showTab('guide', this)">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"></path></svg>
      <span>Инструкция</span>
    </button>
  </aside>

  <main class="main-content">
    {message_html}

    <!-- TAB: OVERVIEW / DASHBOARD -->
    <div id="tab-overview" class="tab-panel active">
      <div class="metrics-grid">
        <div class="metric-box">
          <div class="metric-label">Переслано сообщений</div>
          <div class="metric-val">{status.get('forwarded_count', 0)}</div>
          <div class="metric-sub">Сквозной транзит TG ↔ MAX</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Активные маршруты</div>
          <div class="metric-val">{len(settings.routes)}</div>
          <div class="metric-sub">{html.escape(routes_summary)}</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Антифлуд (Rate Limit)</div>
          <div class="metric-val">{settings.rate_limit_max}</div>
          <div class="metric-sub">Лимит за {settings.rate_limit_window_seconds}с на чат</div>
        </div>
        <div class="metric-box">
          <div class="metric-label">Статус соединения</div>
          <div class="metric-val" style="font-size:1.15rem; margin-top:0.35rem;">
            {"<span style='color:var(--ok)'>Онлайн</span>" if connected else "<span style='color:var(--err)'>Отключен</span>"}
          </div>
          <div class="metric-sub">
            {"<span style='color:var(--err)'>Ошибка: " + html.escape(status.get('last_error') or '') + "</span>" if status.get('last_error') else "Ошибок не зафиксировано"}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Лента последних сообщений</div>
            <div class="card-desc">Хронология пересылки между Telegram и MAX в реальном времени</div>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:90px;">Время</th>
                <th style="width:110px;">Канал</th>
                <th style="width:140px;">Chat ID</th>
                <th>Содержимое</th>
              </tr>
            </thead>
            <tbody>
              {recent_rows}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB: ROUTES -->
    <div id="tab-routes" class="tab-panel">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Маршруты пересылки</div>
            <div class="card-desc">Связывание групп Telegram и чатов MAX для двусторонней трансляции</div>
          </div>
          <div style="display:flex; gap:0.6rem;">
            <button type="button" class="btn btn-secondary" onclick="openChatPicker()">
              🔍 Показать чаты MAX
            </button>
            <button type="button" class="btn btn-secondary" onclick="addRouteRow()">
              + Добавить строку
            </button>
          </div>
        </div>

        <form method="post" action="/save" id="routesForm">
          <div class="table-wrap" style="margin-bottom:1.25rem;">
            <table id="routesTable">
              <thead>
                <tr>
                  <th style="width:30%;">Telegram Chat ID</th>
                  <th style="width:30%;">MAX Chat ID</th>
                  <th>Метка (название)</th>
                  <th style="width:50px;"></th>
                </tr>
              </thead>
              <tbody id="routesListBody">
                {route_rows}
              </tbody>
            </table>
          </div>

          <div style="display:flex; justify-content:space-between; align-items:center;">
            <p class="form-hint">После сохранения перезапустите мост (кнопка вверху или вкладка "Службы systemd"), чтобы применить маршруты.</p>
            <button type="submit" class="btn btn-primary">Сохранить маршруты</button>
          </div>
        </form>
      </div>
    </div>

    <!-- TAB: SIMULATOR (TEST SEND) -->
    <div id="tab-simulator" class="tab-panel">
      <div class="card" style="max-width:700px;">
        <div class="card-header">
          <div>
            <div class="card-title">Тестовая отправка сообщения</div>
            <div class="card-desc">Проверка доставки в чат MAX напрямую через шлюз моста</div>
          </div>
        </div>

        <div class="form-group">
          <label>Целевой чат MAX</label>
          <select id="testTarget" class="form-select">
            {route_options}
          </select>
        </div>

        <div class="form-group">
          <label>Текст сообщения</label>
          <textarea id="testText" class="form-input font-mono" rows="4" style="resize:vertical;">Тестовое сообщение из панели управления Telegram ↔ MAX Bridge</textarea>
        </div>

        <div style="margin-top:1.25rem; display:flex; align-items:center; gap:1rem;">
          <button type="button" class="btn btn-primary" onclick="sendTest()">
            Отправить тестовое сообщение
          </button>
          <div id="sendResult"></div>
        </div>
      </div>
    </div>

    <!-- TAB: LOGS -->
    <div id="tab-logs" class="tab-panel">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Журнал событий (bridge.log)</div>
            <div class="card-desc">Последние записи лога работы моста</div>
          </div>
        </div>
        <pre class="terminal">{html.escape(log_tail)}</pre>
      </div>
    </div>

    <!-- TAB: SETTINGS -->
    <div id="tab-settings" class="tab-panel">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Параметры конфигурации (.env)</div>
            <div class="card-desc">Управление ключами, порогами алертов и параметрами антифлуда</div>
          </div>
        </div>

        <form method="post" action="/save" id="settingsForm">
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:1.25rem;">
            <div class="form-group">
              <label>TELEGRAM_BOT_TOKEN</label>
              <input name="TELEGRAM_BOT_TOKEN" value="{html.escape(_mask(settings.telegram_bot_token))}" class="form-input font-mono" placeholder="оставьте как есть, если не меняете">
              <div class="form-hint">Токен бота от @BotFather (маскируется при чтении)</div>
            </div>

            <div class="form-group">
              <label>ALERT_CHAT_ID (TG ID для алертов)</label>
              <input name="ALERT_CHAT_ID" value="{settings.alert_chat_id or ''}" class="form-input font-mono" placeholder="необязательно">
              <div class="form-hint">Личный чат или канал для оповещений о сбоях</div>
            </div>

            <div class="form-group">
              <label>ALERT_DISCONNECT_SECONDS</label>
              <input name="ALERT_DISCONNECT_SECONDS" value="{settings.alert_disconnect_seconds}" class="form-input font-mono">
              <div class="form-hint">Таймаут разрыва с MAX для отправки алерта</div>
            </div>

            <div class="form-group">
              <label>Антифлуд: максимум сообщений</label>
              <input name="RATE_LIMIT_MAX" value="{settings.rate_limit_max}" class="form-input font-mono">
              <div class="form-hint">Лимит событий на один чат-источник</div>
            </div>

            <div class="form-group">
              <label>Антифлуд: временное окно (секунды)</label>
              <input name="RATE_LIMIT_WINDOW_SECONDS" value="{settings.rate_limit_window_seconds}" class="form-input font-mono">
              <div class="form-hint">Ширина скользящего окна rate-limit</div>
            </div>

            <div class="form-group">
              <label>Обратная пересылка (MAX → Telegram)</label>
              <select name="REVERSE_FORWARD_ENABLED" class="form-select">
                <option value="true" {"selected" if settings.reverse_forward_enabled else ""}>Включено (двусторонний мост)</option>
                <option value="false" {"selected" if not settings.reverse_forward_enabled else ""}>Выключено (только TG → MAX)</option>
              </select>
            </div>

            <div class="form-group">
              <label>Порог диска VPS (%)</label>
              <input name="DISK_ALERT_PERCENT" value="{settings.disk_alert_percent:g}" class="form-input font-mono">
              <div class="form-hint">Алерт при превышении занятости диска</div>
            </div>

            <div class="form-group">
              <label>Порог памяти VPS (%)</label>
              <input name="MEM_ALERT_PERCENT" value="{settings.mem_alert_percent:g}" class="form-input font-mono">
              <div class="form-hint">Алерт при нехватке оперативной памяти</div>
            </div>
          </div>

          <div style="margin-top:1.5rem; display:flex; justify-content:space-between; align-items:center;">
            <p class="form-hint">После сохранения перезапустите мост на вкладке "Службы systemd".</p>
            <button type="submit" class="btn btn-primary">Сохранить настройки</button>
          </div>
        </form>
      </div>
    </div>

    <!-- TAB: MAINTENANCE -->
    <div id="tab-maintenance" class="tab-panel">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Службы systemd</div>
            <div class="card-desc">Управление демонами моста на сервере Linux VPS</div>
          </div>
          <button type="button" class="btn btn-secondary" onclick="loadServiceStatus()">
            Обновить статус
          </button>
        </div>

        <div id="serviceStatus">
          <div class="service-item">
            <div>
              <div class="service-title">telegram-max-bridge</div>
              <div class="service-sub">Основной сервис: Telegram-бот, WebClient MAX, HTTP API</div>
            </div>
            <div style="display:flex; align-items:center; gap:1rem;">
              <span id="status-bridge" class="muted font-mono">проверка...</span>
              <button type="button" class="btn btn-secondary" onclick="restartService('bridge')">Перезапустить</button>
            </div>
          </div>

          <div class="service-item">
            <div>
              <div class="service-title">telegram-max-bridge-web</div>
              <div class="service-sub">Веб-панель управления (порт {settings.webui_port})</div>
            </div>
            <div style="display:flex; align-items:center; gap:1rem;">
              <span id="status-webui" class="muted font-mono">проверка...</span>
              <button type="button" class="btn btn-secondary" onclick="restartService('webui')">Перезапустить</button>
            </div>
          </div>
        </div>

        <div id="restartResult" style="margin-top:0.75rem;"></div>
        <p class="form-hint" style="margin-top:1rem;">
          Для работы перезапуска требуются правила sudoers на сервере (файл <code>deploy/bridge-sudoers</code>). Локально в разработке кнопки возвращают ошибку — это ожидаемо.
        </p>
      </div>
    </div>

    <!-- TAB: GUIDE -->
    <div id="tab-guide" class="tab-panel">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Инструкция по настройке моста</div>
            <div class="card-desc">Пошаговое руководство по связыванию Telegram и MAX</div>
          </div>
        </div>

        <div style="display:flex; flex-direction:column; gap:1rem; font-size:0.85rem; line-height:1.6;">
          <div style="padding:1rem; background:#0c111e; border:1px solid var(--border); border-radius:10px;">
            <b style="color:#60a5fa;">Шаг 1. Создание Telegram-бота</b>
            <p class="muted">Напишите @BotFather в Telegram команду <code>/newbot</code>. Скопируйте полученный токен и вставьте в поле <code>TELEGRAM_BOT_TOKEN</code> на вкладке "Настройки .env".</p>
          </div>

          <div style="padding:1rem; background:#0c111e; border:1px solid var(--border); border-radius:10px;">
            <b style="color:#60a5fa;">Шаг 2. Настройка приватности в Telegram</b>
            <p class="muted">В @BotFather перейдите в <code>/mybots</code> → выберите бота → <b>Bot Settings → Group Privacy → Turn off</b>. Добавьте бота в нужную группу Telegram (если бот уже состоял в группе, удалите его и добавьте заново).</p>
          </div>

          <div style="padding:1rem; background:#0c111e; border:1px solid var(--border); border-radius:10px;">
            <b style="color:#60a5fa;">Шаг 3. Определение Telegram Chat ID</b>
            <p class="muted">Напишите сообщение в группу, затем откройте в браузере: <code>https://api.telegram.org/bot&lt;ТОКЕН&gt;/getUpdates</code>. Найдите <code>"chat":{{"id": -100xxxxxxx}}</code>.</p>
          </div>

          <div style="padding:1rem; background:#0c111e; border:1px solid var(--border); border-radius:10px;">
            <b style="color:#60a5fa;">Шаг 4. Настройка маршрута</b>
            <p class="muted">На вкладке "Маршруты" нажмите "Показать чаты MAX", выберите чат и укажите Telegram Chat ID. Нажмите "Сохранить маршруты" и перезапустите мост.</p>
          </div>
        </div>
      </div>
    </div>

  </main>
</div>

<!-- Modal: MAX Chat Picker -->
<div id="chatModal" class="modal-overlay">
  <div class="modal-card">
    <div class="modal-header">
      <div class="modal-title">Доступные чаты MAX</div>
      <button type="button" class="modal-close" onclick="closeChatPicker()">✕</button>
    </div>
    <div id="chatsResult" style="max-height:350px; overflow-y:auto;">
      <p class="muted">Загрузка...</p>
    </div>
  </div>
</div>

<!-- Toast notification -->
<div id="toast" class="toast-notification">Уведомление</div>

<script>
function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, ch => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }})[ch]);
}}

function showToast(text) {{
  const t = document.getElementById('toast');
  t.textContent = text;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}}

function setMsg(el, color, text) {{
  const p = document.createElement('p');
  if (color) {{ p.style.color = color; }} else {{ p.className = 'muted'; }}
  p.textContent = text;
  el.replaceChildren(p);
}}

function showTab(name, btn) {{
  document.querySelectorAll('.tab-panel').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  const panel = document.getElementById('tab-' + name);
  if (panel) panel.classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'maintenance') {{ loadServiceStatus(); }}
}}

function openChatPicker() {{
  document.getElementById('chatModal').classList.add('active');
  loadChats();
}}

function closeChatPicker() {{
  document.getElementById('chatModal').classList.remove('active');
}}

async function loadChats() {{
  const el = document.getElementById('chatsResult');
  setMsg(el, null, 'Загрузка списка чатов...');
  try {{
    const r = await fetch('/api/chats');
    const data = await r.json();
    if (!r.ok) {{
      setMsg(el, 'var(--err)', 'Ошибка: ' + (data.error || r.status));
      return;
    }}
    if (!data.chats || data.chats.length === 0) {{
      setMsg(el, null, 'Чатов не найдено');
      return;
    }}
    const rows = data.chats.map(c =>
      `<tr><td class="font-mono text-purple">${{esc(c.id)}}</td><td>${{esc(c.type)}}</td><td>${{esc(c.title || '(без названия)')}}</td>` +
      `<td class="text-right"><button type="button" class="btn btn-secondary choose-chat" style="padding:0.3rem 0.6rem; font-size:0.75rem;" data-id="${{esc(c.id)}}" data-title="${{esc(c.title || '')}}">Выбрать</button></td></tr>`
    ).join('');
    el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>ID</th><th>Тип</th><th>Название</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    el.querySelectorAll('.choose-chat').forEach(b =>
      b.addEventListener('click', () => {{
        addRoute(null, b.dataset.id, b.dataset.title);
        closeChatPicker();
        showToast('Чат добавлен в таблицу маршрутов');
      }})
    );
  }} catch (e) {{
    setMsg(el, 'var(--err)', 'Ошибка запроса: ' + e);
  }}
}}

function addRoute(tgId, maxId, label) {{
  const tbody = document.getElementById('routesListBody');
  const tr = document.createElement('tr');
  tr.innerHTML =
    `<td><input name="route_tg" value="${{esc(tgId)}}" class="form-input font-mono" placeholder="ID группы TG"></td>` +
    `<td><input name="route_max" value="${{esc(maxId)}}" class="form-input font-mono" placeholder="ID чата MAX"></td>` +
    `<td><input name="route_label" value="${{esc(label)}}" class="form-input" placeholder="метка (необязательно)"></td>` +
    `<td class="text-right"><button type="button" class="btn-icon-danger" onclick="this.closest('tr').remove()" title="Удалить">✕</button></td>`;
  tbody.appendChild(tr);
}}

function addRouteRow() {{
  addRoute('', '', '');
}}

async function sendTest() {{
  const el = document.getElementById('sendResult');
  const text = document.getElementById('testText').value;
  const maxChatId = document.getElementById('testTarget').value;
  setMsg(el, null, 'Отправка...');
  try {{
    const r = await fetch('/api/test-send', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text, max_chat_id: maxChatId ? parseInt(maxChatId, 10) : null }})
    }});
    const data = await r.json();
    if (!r.ok) {{
      setMsg(el, 'var(--err)', 'Ошибка: ' + (data.error || r.status));
      return;
    }}
    setMsg(el, 'var(--ok)', 'Отправлено успешно');
    showToast('Тестовое сообщение успешно отправлено!');
  }} catch (e) {{
    setMsg(el, 'var(--err)', 'Ошибка запроса: ' + e);
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
      el.style.color = val === 'active' ? 'var(--ok)' : 'var(--err)';
    }}
  }} catch (e) {{
    // тихо игнорируем
  }}
}}

async function restartService(key) {{
  const el = document.getElementById('restartResult');
  setMsg(el, null, 'Перезапуск сервиса...');
  try {{
    const r = await fetch('/api/restart/' + key, {{ method: 'POST' }});
    const data = await r.json();
    if (!r.ok) {{
      setMsg(el, 'var(--err)', 'Ошибка: ' + (data.error || r.status));
      return;
    }}
    setMsg(el, 'var(--ok)', data.note || 'Перезапущено');
    showToast('Команда перезапуска отправлена');
    setTimeout(loadServiceStatus, 3000);
  }} catch (e) {{
    setMsg(el, 'var(--err)', 'Ошибка запроса: ' + e);
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

    # Числовые поля валидируем до записи в .env: мусор там ломает Settings.from_env().
    def _field(name: str) -> str:
        return str(form.get(name, "")).strip()

    if "ALERT_CHAT_ID" in form:
        alert_chat = _field("ALERT_CHAT_ID")
        if not alert_chat:
            values["ALERT_CHAT_ID"] = ""
        elif _is_int(alert_chat):
            values["ALERT_CHAT_ID"] = str(int(alert_chat))

    for name in (
        "ALERT_DISCONNECT_SECONDS",
        "RATE_LIMIT_MAX",
        "RATE_LIMIT_WINDOW_SECONDS",
    ):
        raw = _field(name)
        if raw and _is_int(raw) and int(raw) > 0:
            values[name] = str(int(raw))

    for name in ("DISK_ALERT_PERCENT", "MEM_ALERT_PERCENT"):
        raw = _field(name)
        if raw and _is_percent(raw):
            values[name] = str(float(raw))

    if "REVERSE_FORWARD_ENABLED" in form:
        flag = _field("REVERSE_FORWARD_ENABLED").lower()
        if flag in ("true", "false"):
            values["REVERSE_FORWARD_ENABLED"] = flag

    if "route_tg" in form:
        tg_ids = form.getlist("route_tg")
        max_ids = form.getlist("route_max")
        labels = form.getlist("route_label")
        routes: list[Route] = []
        for i in range(len(tg_ids)):
            tg_raw = str(tg_ids[i]).strip()
            max_raw = str(max_ids[i]).strip() if i < len(max_ids) else ""
            label = clean_label(str(labels[i])) if i < len(labels) else ""
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
