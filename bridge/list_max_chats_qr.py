"""То же самое, что list_max_chats.py, но вход через QR-код вместо SMS.

Полезно, если SMS/push с кодом не приходит на телефон.
"""

from __future__ import annotations

import asyncio

from . import tls_patch  # noqa: F401  (должен идти до импорта pymax)
from pymax import WebClient

from .config import Settings


async def main() -> None:
    settings = Settings.from_env()
    client = WebClient(
        session_name="max_session.db",
        work_dir=settings.max_work_dir,
    )

    @client.on_start()
    async def on_start(c: WebClient) -> None:
        display_name = "?"
        if c.me and c.me.contact.names:
            display_name = c.me.contact.names[0].first_name
        print(f"Вошли как: {display_name} (my id={c.me.contact.id if c.me else '?'})\n")
        chats = await c._app.api.chats.fetch_chats()
        print(f"{'id':>15}  {'type':<12}  {'owner':>12}  title")
        print("-" * 60)
        for chat in chats:
            print(
                f"{chat.id:>15}  {str(chat.type):<12}  {chat.owner:>12}  "
                f"{chat.title or '(без названия)'}"
            )
        await c.stop()

    print("Отсканируйте QR-код ниже в приложении MAX: профиль -> Устройства -> Подключить устройство")
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
