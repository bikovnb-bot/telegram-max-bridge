from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv, set_key

ENV_PATH = os.path.abspath(os.getenv("BRIDGE_ENV_FILE", ".env"))

load_dotenv(ENV_PATH)


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value else None


@dataclass(frozen=True)
class Route:
    telegram_chat_id: int
    max_chat_id: int
    label: str = ""


def _parse_routes(
    raw: str | None, legacy_source: int | None, legacy_target: int | None
) -> list[Route]:
    routes: list[Route] = []
    if raw:
        for part in raw.split(";"):
            part = part.strip()
            if not part:
                continue
            bits = part.split(":")
            if len(bits) < 2:
                continue
            try:
                tg_id = int(bits[0])
                max_id = int(bits[1])
            except ValueError:
                continue
            label = bits[2] if len(bits) > 2 else ""
            routes.append(Route(tg_id, max_id, label))

    if not routes and legacy_source is not None and legacy_target is not None:
        routes.append(Route(legacy_source, legacy_target, "default"))

    return routes


def serialize_routes(routes: list[Route]) -> str:
    return ";".join(f"{r.telegram_chat_id}:{r.max_chat_id}:{r.label}" for r in routes)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    max_phone: str
    max_work_dir: str
    webui_username: str
    webui_password: str
    webui_port: int
    forward_token: str
    forward_port: int
    routes: list[Route]
    alert_chat_id: int | None
    alert_disconnect_seconds: int
    rate_limit_max: int
    rate_limit_window_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        legacy_source = _int_or_none(os.getenv("TELEGRAM_SOURCE_CHAT_ID"))
        legacy_target = _int_or_none(os.getenv("MAX_TARGET_CHAT_ID"))
        routes = _parse_routes(os.getenv("ROUTES"), legacy_source, legacy_target)

        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            max_phone=os.environ.get("MAX_PHONE", ""),
            max_work_dir=os.getenv("MAX_WORK_DIR", "./max_session"),
            webui_username=os.environ.get("WEBUI_USERNAME", "admin"),
            webui_password=os.environ.get("WEBUI_PASSWORD", ""),
            webui_port=int(os.environ.get("WEBUI_PORT", "8765")),
            forward_token=os.environ.get("FORWARD_TOKEN", ""),
            forward_port=int(os.environ.get("FORWARD_PORT", "8766")),
            routes=routes,
            alert_chat_id=_int_or_none(os.getenv("ALERT_CHAT_ID")),
            alert_disconnect_seconds=int(os.environ.get("ALERT_DISCONNECT_SECONDS", "120")),
            rate_limit_max=int(os.environ.get("RATE_LIMIT_MAX", "20")),
            rate_limit_window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")),
        )

    @property
    def status_file(self) -> str:
        return os.path.join(self.max_work_dir, "status.json")

    @property
    def log_file(self) -> str:
        return os.path.join(self.max_work_dir, "bridge.log")

    @property
    def default_max_target(self) -> int | None:
        return self.routes[0].max_chat_id if self.routes else None

    def max_target_for(self, telegram_chat_id: int) -> int | None:
        for route in self.routes:
            if route.telegram_chat_id == telegram_chat_id:
                return route.max_chat_id
        return None


_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "MAX_PHONE",
    "MAX_WORK_DIR",
    "WEBUI_USERNAME",
    "WEBUI_PASSWORD",
    "WEBUI_PORT",
    "FORWARD_TOKEN",
    "FORWARD_PORT",
    "ROUTES",
    "ALERT_CHAT_ID",
    "ALERT_DISCONNECT_SECONDS",
    "RATE_LIMIT_MAX",
    "RATE_LIMIT_WINDOW_SECONDS",
    # legacy имена, оставлены для обратной совместимости при миграции
    "TELEGRAM_SOURCE_CHAT_ID",
    "MAX_TARGET_CHAT_ID",
)


def update_env(values: dict[str, str]) -> None:
    """Обновляет .env файл на диске указанными значениями (только известные ключи)."""
    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "a", encoding="utf-8").close()

    for key, value in values.items():
        if key not in _ENV_KEYS:
            continue
        set_key(ENV_PATH, key, value, quote_mode="never")
