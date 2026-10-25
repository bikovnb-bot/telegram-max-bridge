import pytest
from fastapi.testclient import TestClient

from bridge import config, webui
from bridge.config import Route, clean_env_value, clean_label, serialize_routes, update_env

HOST = "testserver"
AUTH = ("admin", "testpass")


@pytest.fixture(autouse=True)
def isolated_env_path(env_settings, monkeypatch):
    """ENV_PATH вычисляется при импорте — направляем запись в .env из фикстуры."""
    monkeypatch.setattr(config, "ENV_PATH", str(env_settings))


@pytest.fixture
def client(env_settings):
    return TestClient(webui.app, base_url=f"http://{HOST}")


def test_clean_label_strips_separators_and_controls():
    assert clean_label("of:fi;ce\n") == "office"


def test_serialize_routes_cleans_label():
    assert serialize_routes([Route(1, 2, "a:b;c")]) == "1:2:abc"


def test_clean_env_value_strips_newlines():
    assert clean_env_value("1\nEVIL=1\r") == "1EVIL=1"


def test_update_env_blocks_newline_injection(env_settings):
    update_env({"ALERT_CHAT_ID": "123\nFORWARD_TOKEN=evil"})
    content = env_settings.read_text()
    assert "\nFORWARD_TOKEN=evil" not in content
    assert len([ln for ln in content.splitlines() if ln.startswith("FORWARD_TOKEN")]) == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ],
)
def test_cross_origin_post_blocked(client, headers):
    r = client.post("/api/restart/bridge", auth=AUTH, headers=headers)
    assert r.status_code == 403


def test_cross_origin_save_does_not_touch_env(client, env_settings):
    r = client.post(
        "/save",
        auth=AUTH,
        data={"ALERT_CHAT_ID": "999"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "999" not in env_settings.read_text()


def test_same_origin_post_allowed(client, env_settings):
    r = client.post(
        "/save",
        auth=AUTH,
        data={"ALERT_CHAT_ID": "999"},
        headers={"Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ALERT_CHAT_ID=999" in env_settings.read_text()


def test_post_without_browser_headers_allowed(client):
    r = client.post("/api/restart/unknown", auth=AUTH)
    assert r.status_code == 404


def test_security_headers_present(client):
    r = client.get("/", auth=AUTH)
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_save_rejects_garbage_numbers(client, env_settings):
    r = client.post(
        "/save",
        auth=AUTH,
        data={
            "ALERT_CHAT_ID": "abc",
            "RATE_LIMIT_MAX": "-5",
            "DISK_ALERT_PERCENT": "150",
            "REVERSE_FORWARD_ENABLED": "maybe",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    content = env_settings.read_text()
    for key in ("ALERT_CHAT_ID", "RATE_LIMIT_MAX", "DISK_ALERT_PERCENT", "REVERSE_FORWARD_ENABLED"):
        assert key not in content


def test_save_route_label_is_cleaned(client, env_settings):
    client.post(
        "/save",
        auth=AUTH,
        data={"route_tg": ["-100"], "route_max": ["5"], "route_label": ["x:y;z"]},
        follow_redirects=False,
    )
    assert "ROUTES=-100:5:xyz" in env_settings.read_text()


def test_page_escapes_chat_titles_client_side(client):
    html_text = client.get("/", auth=AUTH).text
    assert "function esc(" in html_text
    assert "onclick=\"addRoute(null, ${c.id}" not in html_text
