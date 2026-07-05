"""Логинится в MAX и печатает список чатов с их id, чтобы найти MAX_TARGET_CHAT_ID."""

from __future__ import annotations

import asyncio

from . import tls_patch  # noqa: F401  (должен идти до импорта pymax)
from pymax import Client

from .config import Settings


async def main() -> None:
    settings = Settings.from_env()
    client = Client(
        phone=settings.max_phone,
        session_name="max_session.db",
        work_dir=settings.max_work_dir,
    )

    @client.on_start()
    async def on_start(c: Client) -> None:
        display_name = "?"
        if c.me and c.me.contact.names:
            display_name = c.me.contact.names[0].first_name
        print(f"Вошли как: {display_name}\n")
        print(f"{'id':>15}  title")
        print("-" * 40)
        for chat in c.chats or []:
            print(f"{chat.id:>15}  {chat.title or '(без названия)'}")
        await c.stop()

    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
