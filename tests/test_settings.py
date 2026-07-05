from bridge.config import Route, Settings


def test_settings_from_env_basic(env_settings, monkeypatch):
    monkeypatch.setenv("ROUTES", "-100123:456:office")
    settings = Settings.from_env()

    assert settings.telegram_bot_token == "123456:TEST_TOKEN"
    assert settings.forward_port == 8766
    assert settings.rate_limit_max == 20
    assert settings.routes == [Route(-100123, 456, "office")]
    assert settings.reverse_forward_enabled is True


def test_settings_max_target_for(env_settings, monkeypatch):
    monkeypatch.setenv("ROUTES", "-100123:456:office;-100999:789:home")
    settings = Settings.from_env()

    assert settings.max_target_for(-100123) == 456
    assert settings.max_target_for(-100999) == 789
    assert settings.max_target_for(-999999) is None


def test_settings_telegram_target_for(env_settings, monkeypatch):
    monkeypatch.setenv("ROUTES", "-100123:456:office;-100999:789:home")
    settings = Settings.from_env()

    assert settings.telegram_target_for(456) == -100123
    assert settings.telegram_target_for(789) == -100999
    assert settings.telegram_target_for(111) is None


def test_settings_default_max_target(env_settings, monkeypatch):
    monkeypatch.setenv("ROUTES", "-100123:456:office;-100999:789:home")
    settings = Settings.from_env()
    assert settings.default_max_target == 456


def test_settings_no_routes(env_settings):
    settings = Settings.from_env()
    assert settings.routes == []
    assert settings.default_max_target is None
    assert settings.max_target_for(123) is None


def test_settings_reverse_forward_disabled(env_settings, monkeypatch):
    monkeypatch.setenv("REVERSE_FORWARD_ENABLED", "false")
    settings = Settings.from_env()
    assert settings.reverse_forward_enabled is False
