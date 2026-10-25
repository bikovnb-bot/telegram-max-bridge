"""Очередь исходящих сообщений в MAX: хранение, повторы с паузой, порядок доставки.

Сообщение сначала записывается в базу (`archive.Archive`) со статусом `queued`,
потом фоновый воркер отправляет его в MAX. Пока MAX не подключён или отправка
падает, сообщение остаётся в базе и переживает перезапуск моста. Порядок
доставки сохраняется отдельно для каждого чата-получателя.

Гарантия — «хотя бы один раз»: если мост упал между отправкой и записью
статуса, после перезапуска сообщение может уйти повторно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .archive import Archive

logger = logging.getLogger("bridge.outbox")

# (вид: "photo" | "file", имя файла, содержимое)
Attachment = tuple[str, str, bytes]
SendFn = Callable[[int, str, list[Attachment]], Awaitable[None]]


@dataclass
class SubmitResult:
    id: int
    status: str  # "sent" — доставлено сразу, "queued" — в очереди, будет повтор


class Outbox:
    BACKOFF_SECONDS = (5, 15, 45, 120, 300, 600)
    MAX_ATTEMPTS = 20
    MAX_AGE_SECONDS = 24 * 3600
    POLL_SECONDS = 5

    def __init__(
        self,
        archive: Archive,
        send: SendFn,
        max_ready: asyncio.Event,
        status,
        alerts,
        files_dir: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.archive = archive
        self._send = send
        self.max_ready = max_ready
        self.status = status
        self.alerts = alerts
        self.files_dir = files_dir
        self.clock = clock
        self._waiters: dict[int, asyncio.Future] = {}
        self._wake = asyncio.Event()

    async def submit(
        self,
        chat_id: int,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
        direction: str = "tg->max",
        source_chat: int | None = None,
        author: str = "",
        wait: float = 0.0,
    ) -> SubmitResult:
        """Ставит сообщение в очередь. С wait>0 ждёт первую попытку доставки."""
        msg_id = self.archive.add(
            direction, text,
            source_chat=source_chat, target_chat=chat_id, author=author,
            status="queued", now=self.clock(),
        )
        meta = self._save_attachments(msg_id, attachments or [])
        if meta:
            self.archive.set_attachments(msg_id, meta)

        future: asyncio.Future | None = None
        if wait > 0:
            future = asyncio.get_running_loop().create_future()
            self._waiters[msg_id] = future
        self._wake.set()

        if future is None:
            return SubmitResult(msg_id, "queued")
        try:
            outcome = await asyncio.wait_for(asyncio.shield(future), wait)
        except asyncio.TimeoutError:
            outcome = "queued"
        finally:
            self._waiters.pop(msg_id, None)
        return SubmitResult(msg_id, "sent" if outcome == "sent" else "queued")

    async def run(self) -> None:
        purge_at = 0.0
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process_due()
                now = self.clock()
                if now >= purge_at:
                    removed = self.archive.purge(now)
                    if removed:
                        logger.info("архив: удалено записей старше срока хранения: %s", removed)
                    purge_at = now + 3600
            except Exception:  # noqa: BLE001
                logger.exception("ошибка в обработке очереди, продолжаю")

    async def process_due(self) -> None:
        if not self.max_ready.is_set():
            return
        blocked: set[int | None] = set()
        for row in self.archive.due(self.clock()):
            if not self.max_ready.is_set():
                break
            chat = row["target_chat"]
            if chat in blocked:
                continue
            if self.clock() - row["ts"] > self.MAX_AGE_SECONDS:
                await self._give_up(row, "истёк срок ожидания в очереди")
                continue
            if not await self._deliver(row):
                blocked.add(chat)

    async def _deliver(self, row: dict) -> bool:
        msg_id = row["id"]
        try:
            await self._send(row["target_chat"], row["text"], self._load_attachments(row))
        except Exception as exc:  # noqa: BLE001
            await self._on_failure(row, exc)
            return False

        self.archive.mark_sent(msg_id, self.clock())
        self._drop_files(row)
        self.status.record_forwarded(row["text"], row["target_chat"], direction=row["direction"])
        await self.alerts.on_forward_success()
        self._resolve(msg_id, "sent")
        logger.info("переслано сообщение #%s -> max_chat_id=%s", msg_id, row["target_chat"])
        return True

    async def _on_failure(self, row: dict, exc: Exception) -> None:
        attempts = row["attempts"] + 1
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "не удалось отправить сообщение #%s в MAX (попытка %s): %s",
            row["id"], attempts, error,
        )
        self.status.set_error(error)
        await self.alerts.on_forward_failure(error)

        if attempts >= self.MAX_ATTEMPTS:
            await self._give_up(row, error, attempts)
            return
        delay = self.BACKOFF_SECONDS[min(attempts - 1, len(self.BACKOFF_SECONDS) - 1)]
        self.archive.mark_retry(row["id"], error, self.clock() + delay)
        self._resolve(row["id"], "retry")

    async def _give_up(self, row: dict, error: str, attempts: int | None = None) -> None:
        self.archive.mark_failed(row["id"], error)
        self._drop_files(row)
        self._resolve(row["id"], "failed")
        tries = f" после {attempts} попыток" if attempts else ""
        await self.alerts.notify(
            f"❌ Сообщение #{row['id']} не доставлено в MAX{tries}: {error}"
        )

    def _resolve(self, msg_id: int, outcome: str) -> None:
        future = self._waiters.get(msg_id)
        if future is not None and not future.done():
            future.set_result(outcome)

    def _save_attachments(self, msg_id: int, attachments: list[Attachment]) -> list[dict]:
        meta: list[dict] = []
        if not attachments:
            return meta
        os.makedirs(self.files_dir, exist_ok=True)
        for index, (kind, name, raw) in enumerate(attachments):
            filename = f"{msg_id}_{index}.bin"  # имя из чата не используем в пути
            path = os.path.join(self.files_dir, filename)
            with open(path, "wb") as f:
                f.write(raw)
            os.chmod(path, 0o600)
            meta.append({"kind": kind, "name": name, "file": filename})
        return meta

    def _load_attachments(self, row: dict) -> list[Attachment]:
        result: list[Attachment] = []
        for item in json.loads(row["attachments"] or "[]"):
            filename = item.get("file")
            if not filename:
                continue
            try:
                with open(os.path.join(self.files_dir, os.path.basename(filename)), "rb") as f:
                    result.append((item["kind"], item["name"], f.read()))
            except OSError:
                logger.warning("файл вложения %s не найден, отправляю без него", filename)
        return result

    def _drop_files(self, row: dict) -> None:
        names: list[dict] = []
        for item in json.loads(row["attachments"] or "[]"):
            filename = item.get("file")
            if filename:
                try:
                    os.remove(os.path.join(self.files_dir, os.path.basename(filename)))
                except OSError:
                    pass
            names.append({"kind": item.get("kind"), "name": item.get("name")})
        if names:
            self.archive.set_attachments(row["id"], names)
