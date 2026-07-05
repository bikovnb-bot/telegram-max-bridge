"""Заставляет asyncio использовать актуальный CA-бандл certifi вместо
системного хранилища сертификатов Windows.

Нужно из-за того, что Windows-хранилище может содержать устаревший корневой
сертификат в цепочке Let's Encrypt, из-за чего проверка валидного сертификата
MAX (api.oneme.ru) падает с "certificate has expired", хотя сам сертификат
действителен. certifi решает эту проблему, т.к. содержит только актуальные
корневые CA.
"""

from __future__ import annotations

import ssl

import certifi

_original_create_default_context = ssl.create_default_context


def _certifi_backed_context(*args: object, **kwargs: object) -> ssl.SSLContext:
    kwargs.setdefault("cafile", certifi.where())
    return _original_create_default_context(*args, **kwargs)  # type: ignore[arg-type]


ssl.create_default_context = _certifi_backed_context  # type: ignore[assignment]
