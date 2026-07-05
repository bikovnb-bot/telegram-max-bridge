import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge.config import Route, Settings
from bridge.main import AlertManager, RateLimiter, build_forward_api
from bridge.status import StatusWriter


def make_settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        max_phone="+7",
        max_work_dir="/tmp/x",
        webui_username="admin",
        webui_password="p",
        webui_port=8765,
        forward_token="sekret",
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


@pytest.fixture
def status_writer(tmp_path):
    return StatusWriter(str(tmp_path / "status.json"))


async def make_client(settings, status_writer, rate_limit_max=20):
    max_client = AsyncMock()
    max_client._app.api.messages.send_message = AsyncMock()
    max_client._app.api.chats.fetch_chats = AsyncMock(
        return_value=[
            type("C", (), {"id": 456, "type": "CHAT", "title": "office", "owner": 1})()
        ]
    )
    max_ready = asyncio.Event()
    max_ready.set()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(rate_limit_max, 60)

    app = build_forward_api(max_client, settings, max_ready, status_writer, alerts, rate_limiter)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, max_client


@pytest.mark.asyncio
async def test_forward_requires_auth(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post("/forward", json={"text": "hi"})
        assert r.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_wrong_token(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"}
        )
        assert r.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_missing_text(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={}, headers={"Authorization": "Bearer sekret"}
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_success_uses_default_route(status_writer):
    settings = make_settings()
    client, max_client = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={"text": "hello"}, headers={"Authorization": "Bearer sekret"}
        )
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True
        max_client._app.api.messages.send_message.assert_awaited_once()
        _, kwargs = max_client._app.api.messages.send_message.call_args
        assert kwargs["chat_id"] == 456
        assert kwargs["text"] == "hello"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_explicit_chat_id_overrides_default(status_writer):
    settings = make_settings()
    client, max_client = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward",
            json={"text": "hi", "max_chat_id": 999},
            headers={"Authorization": "Bearer sekret"},
        )
        assert r.status == 200
        _, kwargs = max_client._app.api.messages.send_message.call_args
        assert kwargs["chat_id"] == 999
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_no_default_route_requires_chat_id(status_writer):
    settings = make_settings(routes=[])
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={"text": "hi"}, headers={"Authorization": "Bearer sekret"}
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_rate_limited(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer, rate_limit_max=1)
    try:
        r1 = await client.post(
            "/forward", json={"text": "one"}, headers={"Authorization": "Bearer sekret"}
        )
        r2 = await client.post(
            "/forward", json={"text": "two"}, headers={"Authorization": "Bearer sekret"}
        )
        assert r1.status == 200
        assert r2.status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chats_endpoint_returns_list(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.get("/chats", headers={"Authorization": "Bearer sekret"})
        assert r.status == 200
        data = await r.json()
        assert data["chats"] == [{"id": 456, "type": "CHAT", "title": "office", "owner": 1}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chats_requires_auth(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.get("/chats")
        assert r.status == 401
    finally:
        await client.close()
