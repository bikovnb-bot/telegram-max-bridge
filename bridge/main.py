"""Мост Telegram -> MAX: читает сообщения из Telegram-групп и пересылает их в MAX."""

from __future__ import annotations

import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler

from . import tls_patch  # noqa: F401  (должен идти до импорта pymax)
from aiogram import Bot, Dispatcher
from aiogram.enums import ContentType
from aiogram.filters import Filter
from aiogram.types import Message as TgMessage
from aiohttp import web
from pymax import File, Photo, WebClient

from .config import Settings
from .status import StatusWriter

logger = logging.getLogger("bridge")

FORWARDABLE_CONTENT_TYPES = {ContentType.TEXT, ContentType.PHOTO, ContentType.DOCUMENT}


def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
        ],
    )


class FromKnownRoute(Filter):
    """Пропускает только сообщения из чатов, для которых настроен маршрут."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self, message: TgMessage) -> bool:
        if message.content_type not in FORWARDABLE_CONTENT_TYPES:
            return False
        return self.settings.max_target_for(message.chat.id) is not None


class AlertManager:
    """Следит за отключениями MAX и сбоями пересылки, шлёт алерт в Telegram."""

    def __init__(self, tg_bot: Bot, settings: Settings) -> None:
        self.tg_bot = tg_bot
        self.settings = settings
        self.disconnected_since: float | None = None
        self.disconnect_alert_sent = False
        self.consecutive_failures = 0
        self.failure_alert_sent = False

    async def notify(self, text: str) -> None:
        if not self.settings.alert_chat_id:
            return
        try:
            await self.tg_bot.send_message(self.settings.alert_chat_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось отправить алерт")

    def on_connected(self) -> None:
        if self.disconnected_since is not None and self.disconnect_alert_sent:
            asyncio.create_task(self.notify("✅ Соединение с MAX восстановлено"))
        self.disconnected_since = None
        self.disconnect_alert_sent = False

    def on_disconnected(self) -> None:
        if self.disconnected_since is None:
            self.disconnected_since = time.time()

    async def watch_disconnect(self) -> None:
        while True:
            await asyncio.sleep(15)
            if (
                self.disconnected_since is not None
                and not self.disconnect_alert_sent
                and time.time() - self.disconnected_since >= self.settings.alert_disconnect_seconds
            ):
                self.disconnect_alert_sent = True
                await self.notify(
                    f"⚠️ MAX не подключён уже {self.settings.alert_disconnect_seconds}+ секунд"
                )

    async def on_forward_success(self) -> None:
        if self.failure_alert_sent:
            await self.notify("✅ Пересылка в MAX снова работает")
        self.consecutive_failures = 0
        self.failure_alert_sent = False

    async def on_forward_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3 and not self.failure_alert_sent:
            self.failure_alert_sent = True
            await self.notify(f"⚠️ Пересылка в MAX падает подряд: {error}")


class RateLimiter:
    """Простой sliding-window rate limiter на ключ (например, id чата-источника).

    Защищает от шквала сообщений (баг на сайте, спам в группе) — лишние
    сообщения выше max_events за window_seconds просто отбрасываются.
    """

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._last_alert: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        events = self._events.setdefault(key, [])
        while events and now - events[0] > self.window_seconds:
            events.pop(0)
        if len(events) >= self.max_events:
            return False
        events.append(now)
        return True

    def should_alert(self, key: str) -> bool:
        """Не чаще одного алерта за window_seconds на ключ."""
        now = time.time()
        last = self._last_alert.get(key, 0.0)
        if now - last >= self.window_seconds:
            self._last_alert[key] = now
            return True
        return False


async def send_to_max(
    max_client: WebClient,
    settings: Settings,
    max_ready: asyncio.Event,
    status: StatusWriter,
    alerts: AlertManager,
    text: str,
    chat_id: int,
    attachments: list | None = None,
) -> None:
    logger.info("ожидание готовности соединения с MAX")
    await max_ready.wait()

    try:
        await max_client._app.api.messages.send_message(
            chat_id=chat_id,
            text=text,
            attachments=attachments or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("не удалось переслать сообщение в MAX")
        status.set_error(str(exc))
        await alerts.on_forward_failure(str(exc))
        raise

    status.record_forwarded(text, chat_id)
    await alerts.on_forward_success()
    logger.info("переслано сообщение -> max_chat_id=%s", chat_id)


async def _build_attachments(message: TgMessage) -> list:
    attachments: list = []
    if message.photo:
        largest = message.photo[-1]
        buf = await message.bot.download(largest)
        attachments.append(Photo(raw=buf.read(), name="photo.jpg"))
    elif message.document:
        buf = await message.bot.download(message.document)
        name = message.document.file_name or "file"
        attachments.append(File(raw=buf.read(), name=name))
    return attachments


def build_forwarder(
    max_client: WebClient,
    settings: Settings,
    max_ready: asyncio.Event,
    status: StatusWriter,
    alerts: AlertManager,
    rate_limiter: RateLimiter,
):
    async def forward(message: TgMessage) -> None:
        target_chat_id = settings.max_target_for(message.chat.id)
        if target_chat_id is None:
            return

        rate_key = f"tg:{message.chat.id}"
        if not rate_limiter.allow(rate_key):
            logger.warning(
                "rate limit: сообщение из chat_id=%s отброшено (лимит %s за %sс)",
                message.chat.id, rate_limiter.max_events, rate_limiter.window_seconds,
            )
            if rate_limiter.should_alert(rate_key):
                asyncio.create_task(
                    alerts.notify(
                        f"⚠️ Превышен лимит сообщений из Telegram-чата {message.chat.id} "
                        f"({rate_limiter.max_events}/{rate_limiter.window_seconds}с) — часть сообщений отброшена"
                    )
                )
            return

        text = message.text or message.caption or ""

        try:
            attachments = await _build_attachments(message)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось скачать вложение из Telegram")
            attachments = []

        if not text and not attachments:
            return

        author = message.from_user.full_name if message.from_user else "Telegram"
        forwarded_text = f"{author}: {text}" if text else author

        try:
            await send_to_max(
                max_client, settings, max_ready, status, alerts,
                forwarded_text, target_chat_id, attachments=attachments,
            )
        except Exception:  # noqa: BLE001
            pass

    return forward


def build_forward_api(
    max_client: WebClient,
    settings: Settings,
    max_ready: asyncio.Event,
    status: StatusWriter,
    alerts: AlertManager,
    rate_limiter: RateLimiter,
) -> web.Application:
    """HTTP API для прямой пересылки в MAX, в обход Telegram-группы.

    Нужен, т.к. Telegram-боты не получают сообщения других ботов в группах —
    уведомления от бота приложения не долетают до бота-моста через getUpdates.
    """

    def _check_auth(request: web.Request) -> web.Response | None:
        if not settings.forward_token:
            return web.json_response(
                {"error": "FORWARD_TOKEN не настроен на сервере"}, status=500
            )
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {settings.forward_token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def handle_forward(request: web.Request) -> web.Response:
        error = _check_auth(request)
        if error is not None:
            return error

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)

        text = payload.get("text")
        if not text:
            return web.json_response({"error": "'text' is required"}, status=400)

        chat_id = payload.get("max_chat_id") or settings.default_max_target
        if chat_id is None:
            return web.json_response(
                {"error": "max_chat_id не передан и нет маршрутов по умолчанию"}, status=400
            )

        rate_key = f"http:{chat_id}"
        if not rate_limiter.allow(rate_key):
            logger.warning(
                "rate limit: HTTP-сообщение для max_chat_id=%s отброшено (лимит %s за %sс)",
                chat_id, rate_limiter.max_events, rate_limiter.window_seconds,
            )
            if rate_limiter.should_alert(rate_key):
                asyncio.create_task(
                    alerts.notify(
                        f"⚠️ Превышен лимит HTTP-пересылок для чата MAX {chat_id} "
                        f"({rate_limiter.max_events}/{rate_limiter.window_seconds}с)"
                    )
                )
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        try:
            await send_to_max(
                max_client, settings, max_ready, status, alerts, text, chat_id
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

        return web.json_response({"ok": True})

    async def handle_chats(request: web.Request) -> web.Response:
        error = _check_auth(request)
        if error is not None:
            return error

        await max_ready.wait()
        try:
            chats = await max_client._app.api.chats.fetch_chats()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

        return web.json_response(
            {
                "chats": [
                    {
                        "id": chat.id,
                        "type": str(chat.type),
                        "title": chat.title,
                        "owner": chat.owner,
                    }
                    for chat in chats
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/forward", handle_forward)
    app.router.add_get("/chats", handle_chats)
    return app


async def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_file)
    status = StatusWriter(settings.status_file)

    max_client = WebClient(
        session_name="max_session.db",
        work_dir=settings.max_work_dir,
    )

    max_ready = asyncio.Event()
    tg_bot = Bot(token=settings.telegram_bot_token)
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(settings.rate_limit_max, settings.rate_limit_window_seconds)

    @max_client.on_start()
    async def on_max_start(c: WebClient) -> None:
        logger.info("соединение с MAX установлено")
        status.set_connected(True)
        max_ready.set()
        alerts.on_connected()

    @max_client.on_disconnect()
    async def on_max_disconnect(*_args: object) -> None:
        logger.warning("соединение с MAX разорвано, ждём переподключения")
        status.set_connected(False)
        max_ready.clear()
        alerts.on_disconnected()

    tg_dp = Dispatcher()
    tg_dp.message.register(
        build_forwarder(max_client, settings, max_ready, status, alerts, rate_limiter),
        FromKnownRoute(settings),
    )

    forward_app = build_forward_api(max_client, settings, max_ready, status, alerts, rate_limiter)
    runner = web.AppRunner(forward_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.forward_port)
    await site.start()
    logger.info("HTTP API для прямой пересылки слушает на порту %s", settings.forward_port)

    logger.info(
        "запуск моста Telegram -> MAX, маршрутов: %s",
        len(settings.routes),
    )
    await asyncio.gather(
        max_client.start(),
        tg_dp.start_polling(tg_bot),
        alerts.watch_disconnect(),
    )


if __name__ == "__main__":
    asyncio.run(main())
