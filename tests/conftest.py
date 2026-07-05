import os

import pytest


@pytest.fixture
def env_settings(tmp_path, monkeypatch):
    """Настраивает изолированное окружение для Settings.from_env() в тестах."""
    env_file = tmp_path / ".env"
    env_file.write_text("")

    monkeypatch.setenv("BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("MAX_PHONE", "+70000000000")
    monkeypatch.setenv("MAX_WORK_DIR", str(tmp_path / "max_session"))
    monkeypatch.setenv("WEBUI_USERNAME", "admin")
    monkeypatch.setenv("WEBUI_PASSWORD", "testpass")
    monkeypatch.setenv("WEBUI_PORT", "8765")
    monkeypatch.setenv("FORWARD_TOKEN", "test-forward-token")
    monkeypatch.setenv("FORWARD_PORT", "8766")
    monkeypatch.delenv("ROUTES", raising=False)
    monkeypatch.delenv("TELEGRAM_SOURCE_CHAT_ID", raising=False)
    monkeypatch.delenv("MAX_TARGET_CHAT_ID", raising=False)
    monkeypatch.delenv("ALERT_CHAT_ID", raising=False)
    monkeypatch.setenv("ALERT_DISCONNECT_SECONDS", "120")
    monkeypatch.setenv("RATE_LIMIT_MAX", "20")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("REVERSE_FORWARD_ENABLED", "true")

    return env_file
