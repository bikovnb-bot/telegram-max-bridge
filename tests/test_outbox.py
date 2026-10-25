import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from bridge.archive import Archive, count_by_status, query_messages
from bridge.outbox import Outbox


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStatus:
    def __init__(self) -> None:
        self.forwarded: list[tuple] = []
        self.errors: list[str] = []

    def record_forwarded(self, text, chat_id, direction="tg->max") -> None:
        self.forwarded.append((text, chat_id, direction))

    def set_error(self, message) -> None:
        self.errors.append(message)


class FakeAlerts:
    def __init__(self) -> None:
        self.notified: list[str] = []
        self.failures = 0
        self.successes = 0

    async def notify(self, text) -> None:
        self.notified.append(text)

    async def on_forward_failure(self, error) -> None:
        self.failures += 1

    async def on_forward_success(self) -> None:
        self.successes += 1


@pytest.fixture
def setup(tmp_path):
    clock = FakeClock()
    archive = Archive(str(tmp_path / "archive.db"))
    sent: list[tuple] = []
    send = AsyncMock()

    async def record(chat_id, text, attachments):
        await send(chat_id, text, attachments)
        sent.append((chat_id, text, attachments))

    ready = asyncio.Event()
    ready.set()
    status, alerts = FakeStatus(), FakeAlerts()
    outbox = Outbox(
        archive, record, ready, status, alerts, str(tmp_path / "outbox"), clock=clock
    )

    class Ctx:
        pass

    ctx = Ctx()
    ctx.clock, ctx.archive, ctx.sent, ctx.send = clock, archive, sent, send
    ctx.ready, ctx.status, ctx.alerts, ctx.outbox = ready, status, alerts, outbox
    ctx.tmp_path = tmp_path
    yield ctx
    archive.close()


@pytest.mark.asyncio
async def test_message_is_sent_and_archived(setup):
    result = await setup.outbox.submit(10, "привет", author="Ann", source_chat=-1)
    await setup.outbox.process_due()
    assert [(c, t) for c, t, _ in setup.sent] == [(10, "привет")]
    row = setup.archive.get(result.id)
    assert row["status"] == "sent" and row["author"] == "Ann" and row["source_chat"] == -1
    assert setup.status.forwarded == [("привет", 10, "tg->max")]
    assert setup.alerts.successes == 1


@pytest.mark.asyncio
async def test_waits_while_max_is_not_ready_without_burning_attempts(setup):
    setup.ready.clear()
    result = await setup.outbox.submit(10, "ждёт")
    await setup.outbox.process_due()
    assert setup.sent == []
    assert setup.archive.get(result.id)["attempts"] == 0

    setup.ready.set()
    await setup.outbox.process_due()
    assert len(setup.sent) == 1


@pytest.mark.asyncio
async def test_failed_send_is_retried_after_backoff(setup):
    setup.send.side_effect = [RuntimeError("сеть"), None]
    result = await setup.outbox.submit(10, "повтор")

    await setup.outbox.process_due()
    row = setup.archive.get(result.id)
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "сеть" in row["error"]
    assert setup.alerts.failures == 1

    await setup.outbox.process_due()  # пауза ещё не прошла
    assert setup.send.await_count == 1

    setup.clock.advance(Outbox.BACKOFF_SECONDS[0] + 1)
    await setup.outbox.process_due()
    assert setup.archive.get(result.id)["status"] == "sent"
    assert setup.send.await_count == 2


@pytest.mark.asyncio
async def test_later_message_does_not_overtake_one_waiting_for_retry(setup):
    setup.send.side_effect = [RuntimeError("сбой"), None, None, None]
    first = await setup.outbox.submit(10, "первое")
    await setup.outbox.process_due()  # первое упало, ждёт паузу
    second = await setup.outbox.submit(10, "второе")
    other_chat = await setup.outbox.submit(20, "другой чат")

    await setup.outbox.process_due()
    delivered = [(c, t) for c, t, _ in setup.sent]
    assert (10, "второе") not in delivered  # не обгоняет первое
    assert (20, "другой чат") in delivered  # другие чаты не блокируются

    setup.clock.advance(Outbox.BACKOFF_SECONDS[0] + 1)
    await setup.outbox.process_due()
    order = [t for _, t, _ in setup.sent]
    assert order.index("первое") < order.index("второе")
    assert setup.archive.get(first.id)["status"] == "sent"
    assert setup.archive.get(second.id)["status"] == "sent"
    assert setup.archive.get(other_chat.id)["status"] == "sent"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_alerts(setup):
    setup.send.side_effect = RuntimeError("навсегда")
    result = await setup.outbox.submit(10, "безнадёжно")
    for _ in range(Outbox.MAX_ATTEMPTS + 2):
        await setup.outbox.process_due()
        setup.clock.advance(700)
    row = setup.archive.get(result.id)
    assert row["status"] == "failed"
    assert row["attempts"] == Outbox.MAX_ATTEMPTS
    assert any(f"#{result.id}" in text for text in setup.alerts.notified)


@pytest.mark.asyncio
async def test_expires_after_max_age_when_max_was_down(setup):
    setup.ready.clear()
    result = await setup.outbox.submit(10, "протухло")
    setup.clock.advance(Outbox.MAX_AGE_SECONDS + 1)
    setup.ready.set()
    await setup.outbox.process_due()
    assert setup.sent == []
    assert setup.archive.get(result.id)["status"] == "failed"


@pytest.mark.asyncio
async def test_queue_survives_restart(setup):
    setup.ready.clear()
    result = await setup.outbox.submit(10, "переживёт")

    reborn = Outbox(
        setup.archive, setup.outbox._send, setup.ready, setup.status, setup.alerts,
        str(setup.tmp_path / "outbox"), clock=setup.clock,
    )
    setup.ready.set()
    await reborn.process_due()
    assert [t for _, t, _ in setup.sent] == ["переживёт"]
    assert setup.archive.get(result.id)["status"] == "sent"


@pytest.mark.asyncio
async def test_attachments_are_persisted_then_removed(setup):
    setup.ready.clear()
    result = await setup.outbox.submit(
        10, "с файлом", attachments=[("file", "../evil/отчёт.pdf", b"PDFDATA")]
    )
    outbox_dir = setup.tmp_path / "outbox"
    stored = os.listdir(outbox_dir)
    assert stored == [f"{result.id}_0.bin"]  # имя из чата не попало в путь

    setup.ready.set()
    await setup.outbox.process_due()
    assert setup.sent[0][2] == [("file", "../evil/отчёт.pdf", b"PDFDATA")]
    assert os.listdir(outbox_dir) == []
    import json

    meta = json.loads(setup.archive.get(result.id)["attachments"])
    assert meta == [{"kind": "file", "name": "../evil/отчёт.pdf"}]  # путь к файлу не хранится


@pytest.mark.asyncio
async def test_submit_with_wait_reports_sent_when_worker_runs(setup):
    worker = asyncio.ensure_future(setup.outbox.run())
    try:
        result = await setup.outbox.submit(10, "сразу", wait=2.0)
        assert result.status == "sent"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_submit_with_wait_reports_queued_on_first_failure(setup):
    setup.send.side_effect = RuntimeError("сбой")
    worker = asyncio.ensure_future(setup.outbox.run())
    try:
        result = await setup.outbox.submit(10, "не сразу", wait=2.0)
        assert result.status == "queued"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def test_reader_sees_messages_from_another_connection(setup):
    setup.archive.add("tg->max", "виден", target_chat=1, status="sent")
    rows, total = query_messages(setup.archive.path)
    assert total == 1 and rows[0]["text"] == "виден"
    assert count_by_status(setup.archive.path) == {"queued": 0, "sent": 1, "failed": 0}
