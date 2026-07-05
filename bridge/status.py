"""Простое хранение статуса моста в JSON-файле для веб-интерфейса."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class BridgeStatus:
    max_connected: bool = False
    max_connected_at: float | None = None
    forwarded_count: int = 0
    last_forwarded_at: float | None = None
    last_error: str | None = None
    recent: list[dict] = field(default_factory=list)


class StatusWriter:
    def __init__(self, path: str, keep_recent: int = 20) -> None:
        self._path = path
        self._status = BridgeStatus()
        self._recent: deque[dict] = deque(maxlen=keep_recent)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def set_connected(self, connected: bool) -> None:
        self._status.max_connected = connected
        if connected:
            self._status.max_connected_at = time.time()
        self._flush()

    def set_error(self, message: str) -> None:
        self._status.last_error = message
        self._flush()

    def record_forwarded(self, text: str, max_chat_id: int) -> None:
        self._status.forwarded_count += 1
        self._status.last_forwarded_at = time.time()
        self._recent.appendleft(
            {
                "time": time.time(),
                "text": text[:200],
                "max_chat_id": max_chat_id,
            }
        )
        self._status.recent = list(self._recent)
        self._flush()

    def _flush(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(asdict(self._status), f, ensure_ascii=False, indent=2)


def read_status(path: str) -> dict:
    if not os.path.exists(path):
        return asdict(BridgeStatus())
    with open(path, encoding="utf-8") as f:
        return json.load(f)
