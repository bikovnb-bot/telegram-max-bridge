import csv
import io

import pytest
from fastapi.testclient import TestClient

from bridge import config, webui
from bridge.archive import Archive
from bridge.config import Settings

AUTH = ("admin", "testpass")


@pytest.fixture(autouse=True)
def isolated_env_path(env_settings, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", str(env_settings))


@pytest.fixture
def archive(env_settings):
    a = Archive(Settings.from_env().archive_file)
    yield a
    a.close()


@pytest.fixture
def client(env_settings):
    return TestClient(webui.app, base_url="http://testserver")


def test_archive_requires_auth(client):
    assert client.get("/api/archive").status_code == 401
    assert client.get("/archive.csv").status_code == 401


def test_archive_api_returns_rows_counts_and_attachment_names(client, archive):
    msg_id = archive.add("tg->max", "привет", author="Анна", target_chat=5, status="sent")
    archive.set_attachments(msg_id, [{"kind": "file", "name": "отчёт.pdf"}])
    archive.add("http->max", "ждёт", target_chat=5, status="queued")

    data = client.get("/api/archive", auth=AUTH).json()
    assert data["total"] == 2
    assert data["counts"] == {"queued": 1, "sent": 1, "failed": 0}
    by_text = {r["text"]: r for r in data["rows"]}
    assert by_text["привет"]["attachments"] == ["отчёт.pdf"]
    assert by_text["ждёт"]["status"] == "queued"


def test_archive_api_filters_and_paginates(client, archive):
    for i in range(5):
        archive.add("tg->max", f"msg {i}", status="sent", target_chat=1)
    archive.add("max->tg", "обратное", status="sent", target_chat=2)

    data = client.get("/api/archive?direction=max->tg", auth=AUTH).json()
    assert data["total"] == 1 and data["rows"][0]["text"] == "обратное"

    data = client.get("/api/archive?per_page=2&page=2&direction=tg->max", auth=AUTH).json()
    assert data["total"] == 5 and len(data["rows"]) == 2 and data["page"] == 2


def test_archive_api_without_database_is_empty(client):
    data = client.get("/api/archive", auth=AUTH).json()
    assert data["total"] == 0 and data["rows"] == []


def test_archive_does_not_alter_hostile_text(client, archive):
    payload = '<img src=x onerror="alert(1)">'
    archive.add("tg->max", payload, author="<b>x</b>", status="sent", target_chat=1)
    row = client.get("/api/archive", auth=AUTH).json()["rows"][0]
    assert row["text"] == payload  # хранится как есть, экранирует интерфейс при показе


def test_csv_export_neutralises_formulas(client, archive):
    archive.add("tg->max", "=HYPERLINK(\"http://evil\")", author="+cmd", status="sent", target_chat=1)
    archive.add("tg->max", "обычный", author="Анна", status="sent", target_chat=1)

    r = client.get("/archive.csv", auth=AUTH)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][0] == "id"
    texts = {row[6] for row in rows[1:]}
    assert "'=HYPERLINK(\"http://evil\")" in texts
    assert "обычный" in texts
    authors = {row[5] for row in rows[1:]}
    assert "'+cmd" in authors


def test_csv_export_respects_filters(client, archive):
    archive.add("tg->max", "первое", status="sent", target_chat=1)
    archive.add("max->tg", "второе", status="sent", target_chat=2)
    body = client.get("/archive.csv?direction=max->tg", auth=AUTH).content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert [r[6] for r in rows[1:]] == ["второе"]


def test_archive_tab_is_in_page(client):
    html_text = client.get("/", auth=AUTH).text
    assert 'id="tab-archive"' in html_text
    assert "function loadArchive(" in html_text


def test_retention_setting_validated_on_save(client, env_settings):
    client.post("/save", auth=AUTH, data={"ARCHIVE_RETENTION_DAYS": "abc"}, follow_redirects=False)
    client.post("/save", auth=AUTH, data={"ARCHIVE_RETENTION_DAYS": "-3"}, follow_redirects=False)
    assert "ARCHIVE_RETENTION_DAYS" not in env_settings.read_text()
    client.post("/save", auth=AUTH, data={"ARCHIVE_RETENTION_DAYS": "30"}, follow_redirects=False)
    assert "ARCHIVE_RETENTION_DAYS=30" in env_settings.read_text()


def test_bind_hosts_have_safe_overrides(monkeypatch, env_settings):
    assert Settings.from_env().webui_bind_host == "0.0.0.0"
    monkeypatch.setenv("WEBUI_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("FORWARD_BIND_HOST", "127.0.0.1")
    settings = Settings.from_env()
    assert settings.webui_bind_host == "127.0.0.1"
    assert settings.forward_bind_host == "127.0.0.1"
