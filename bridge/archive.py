"""Архив пересланных сообщений и очередь недоставленных (SQLite).

Одна таблица `messages` служит и архивом (все пересланные сообщения во всех
направлениях), и очередью повторов: статус `queued` — ещё не доставлено,
`sent` — доставлено, `failed` — доставить не удалось. Мост пишет в базу,
веб-интерфейс только читает (режим WAL позволяет читать параллельно).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

DIRECTIONS = ("tg->max", "max->tg", "http->max")
STATUSES = ("queued", "sent", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    source_chat INTEGER,
    target_chat INTEGER,
    author TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(status, next_attempt);
"""

_COLUMNS = (
    "id, ts, direction, source_chat, target_chat, author, text, attachments, "
    "status, error, attempts, next_attempt, sent_at"
)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _query(
    db: sqlite3.Connection,
    limit: int,
    offset: int,
    direction: str | None,
    status: str | None,
    query: str | None,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[object] = []
    if direction in DIRECTIONS:
        where.append("direction = ?")
        params.append(direction)
    if status in STATUSES:
        where.append("status = ?")
        params.append(status)
    if query:
        where.append("(text LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
        pattern = _like_pattern(query)
        params.extend([pattern, pattern])
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = db.execute(f"SELECT COUNT(*) FROM messages {clause}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM messages {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    return [dict(row) for row in rows], total


def query_messages(
    path: str,
    limit: int = 50,
    offset: int = 0,
    direction: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> tuple[list[dict], int]:
    """Читает архив из отдельного процесса (веб-интерфейс), не создавая файл."""
    if not os.path.exists(path):
        return [], 0
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        return _query(db, limit, offset, direction, status, query)
    except sqlite3.OperationalError:
        return [], 0
    finally:
        db.close()


def count_by_status(path: str) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    if not os.path.exists(path):
        return counts
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        for status, n in db.execute("SELECT status, COUNT(*) FROM messages GROUP BY status"):
            counts[status] = n
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()
    return counts


class Archive:
    def __init__(self, path: str, retention_days: int = 90) -> None:
        self.path = path
        self.retention_days = retention_days
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._db.close()

    def add(
        self,
        direction: str,
        text: str,
        *,
        source_chat: int | None = None,
        target_chat: int | None = None,
        author: str = "",
        attachments: list[dict] | None = None,
        status: str = "queued",
        error: str | None = None,
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else now
        cur = self._db.execute(
            "INSERT INTO messages (ts, direction, source_chat, target_chat, author, text, "
            "attachments, status, error, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, direction, source_chat, target_chat, author, text,
                json.dumps(attachments or [], ensure_ascii=False), status, error,
                ts if status == "sent" else None,
            ),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def set_attachments(self, message_id: int, attachments: list[dict]) -> None:
        self._db.execute(
            "UPDATE messages SET attachments = ? WHERE id = ?",
            (json.dumps(attachments, ensure_ascii=False), message_id),
        )
        self._db.commit()

    def get(self, message_id: int) -> dict | None:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def due(self, now: float, limit: int = 50) -> list[dict]:
        """Недоставленные сообщения, которым пора повторить отправку, по порядку.

        Сообщение пропускается, если в тот же чат есть более раннее, которое
        ещё ждёт паузы между попытками, — иначе порядок доставки нарушился бы.
        """
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM messages m WHERE m.status = 'queued' "
            "AND m.next_attempt <= ? "
            "AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.status = 'queued' "
            "AND p.target_chat IS m.target_chat AND p.id < m.id AND p.next_attempt > ?) "
            "ORDER BY m.id LIMIT ?",
            (now, now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, message_id: int, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        self._db.execute(
            "UPDATE messages SET status = 'sent', sent_at = ?, error = NULL, "
            "attempts = attempts + 1 WHERE id = ?",
            (ts, message_id),
        )
        self._db.commit()

    def mark_retry(self, message_id: int, error: str, next_attempt: float) -> None:
        self._db.execute(
            "UPDATE messages SET error = ?, attempts = attempts + 1, next_attempt = ? "
            "WHERE id = ?",
            (error[:500], next_attempt, message_id),
        )
        self._db.commit()

    def mark_failed(self, message_id: int, error: str) -> None:
        self._db.execute(
            "UPDATE messages SET status = 'failed', error = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (error[:500], message_id),
        )
        self._db.commit()

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        direction: str | None = None,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict], int]:
        return _query(self._db, limit, offset, direction, status, query)

    def purge(self, now: float | None = None) -> int:
        """Удаляет доставленные и проваленные записи старше срока хранения."""
        if self.retention_days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - self.retention_days * 86400
        cur = self._db.execute(
            "DELETE FROM messages WHERE status IN ('sent', 'failed') AND ts < ?", (cutoff,)
        )
        self._db.commit()
        return cur.rowcount
