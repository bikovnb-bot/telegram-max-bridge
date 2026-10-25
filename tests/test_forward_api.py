import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge.archive import Archive
from bridge.config import Route, Settings
from bridge.main import AlertManager, RateLimiter, build_forward_api, make_max_sender
from bridge.outbox import Outbox
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


async def make_client(settings, status_writer, rate_limit_max=20, max_ready_set=True):
    max_client = AsyncMock()
    max_client._app.api.messages.send_message = AsyncMock()
    max_client._app.api.chats.fetch_chats = AsyncMock(
        return_value=[
            type("C", (), {"id": 456, "type": "CHAT", "title": "office", "owner": 1})()
        ]
    )
    max_ready = asyncio.Event()
    if max_ready_set:
        max_ready.set()
    tg_bot = AsyncMock()
    alerts = AlertManager(tg_bot, settings)
    rate_limiter = RateLimiter(rate_limit_max, 60)

    workdir = tempfile.mkdtemp()
    archive = Archive(f"{workdir}/archive.db")
    outbox = Outbox(
        archive, make_max_sender(max_client), max_ready, status_writer, alerts, f"{workdir}/outbox"
    )
    worker = asyncio.ensure_future(outbox.run())

    app = build_forward_api(
        max_client, settings, max_ready, status_writer, alerts, rate_limiter, outbox
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    original_close = client.close

    async def close() -> None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await original_close()
        archive.close()

    client.close = close
    client.archive = archive
    client.max_ready = max_ready
    client.outbox = outbox
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


@pytest.mark.asyncio
async def test_forward_token_comparison_handles_non_ascii(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={"text": "hi"},
            headers={"Authorization": "Bearer токен".encode("utf-8").decode("latin-1")},
        )
        assert r.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_when_max_down_returns_202_and_delivers_later(status_writer, monkeypatch):
    import bridge.main as bridge_main

    monkeypatch.setattr(bridge_main, "HTTP_DELIVERY_WAIT_SECONDS", 0.2)
    settings = make_settings()
    client, max_client = await make_client(settings, status_writer, max_ready_set=False)
    try:
        r = await client.post(
            "/forward", json={"text": "позже"}, headers={"Authorization": "Bearer sekret"}
        )
        assert r.status == 202
        data = await r.json()
        assert data["ok"] is True and data["queued"] is True
        max_client._app.api.messages.send_message.assert_not_awaited()

        client.max_ready.set()
        client.outbox._wake.set()
        for _ in range(50):
            if max_client._app.api.messages.send_message.await_count:
                break
            await asyncio.sleep(0.05)
        max_client._app.api.messages.send_message.assert_awaited_once()
        rows, _ = client.archive.list()
        assert rows[0]["status"] == "sent" and rows[0]["direction"] == "http->max"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forward_success_is_archived(status_writer):
    settings = make_settings()
    client, _ = await make_client(settings, status_writer)
    try:
        r = await client.post(
            "/forward", json={"text": "в архив"}, headers={"Authorization": "Bearer sekret"}
        )
        assert r.status == 200
        rows, total = client.archive.list()
        assert total == 1
        assert rows[0]["text"] == "в архив" and rows[0]["status"] == "sent"
        assert rows[0]["target_chat"] == 456
    finally:
        await client.close()
