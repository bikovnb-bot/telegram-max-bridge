import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.config import Route, Settings
from bridge.main import AlertManager, RateLimiter, build_max_forwarder
from bridge.status import StatusWriter


def make_settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        max_phone="+7",
        max_work_dir="/tmp/x",
        webui_username="admin",
        webui_password="p",
        webui_port=8765,
        forward_token="tok",
        forward_port=8766,
        routes=[Route(-100123, 456, "office")],
        alert_chat_id=None,
        alert_disconnect_seconds=120,
        rate_limit_max=20,
        rate_limit_window_seconds=60,
        reverse_forward_enabled=True,
        host_monitor_enabled=False,
        disk_alert_percent=90.0,
        mem_alert_percent=90.0,
        host_monitor_interval_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


def make_event(chat_id=456, sender=999, text="hello"):
    return SimpleNamespace(chat_id=chat_id, sender=sender, text=text)


@pytest.fixture
def status_writer(tmp_path):
    return StatusWriter(str(tmp_path / "status.json"))


@pytest.mark.asyncio
async def test_forwards_message_from_max_to_telegram(status_writer):
    settings = make_settings()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(20, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=456, sender=999, text="привет"), max_client)

    tg_bot.send_message.assert_awaited_once_with(-100123, "[MAX] привет")


@pytest.mark.asyncio
async def test_does_not_echo_own_messages(status_writer):
    settings = make_settings()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(20, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=456, sender=1, text="эхо"), max_client)

    tg_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_unknown_chat(status_writer):
    settings = make_settings()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(20, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=999999, sender=999, text="hi"), max_client)

    tg_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_via_settings(status_writer):
    settings = make_settings(reverse_forward_enabled=False)
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(20, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=456, sender=999, text="hi"), max_client)

    tg_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_text_ignored(status_writer):
    settings = make_settings()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(20, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=456, sender=999, text=""), max_client)

    tg_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited(status_writer):
    settings = make_settings(rate_limit_max=1)
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(1, 60)
    max_client = MagicMock()
    max_client.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    handler = build_max_forwarder(tg_bot, settings, status_writer, alerts, rate_limiter, max_client)
    await handler(make_event(chat_id=456, sender=999, text="one"), max_client)
    await handler(make_event(chat_id=456, sender=999, text="two"), max_client)

    assert tg_bot.send_message.await_count == 1
