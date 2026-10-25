import os
import stat
import sys

import pytest

from bridge.archive import Archive, query_messages


@pytest.fixture
def archive(tmp_path):
    a = Archive(str(tmp_path / "archive.db"), retention_days=30)
    yield a
    a.close()


def fill(archive):
    archive.add("tg->max", "привет мир", author="Анна", target_chat=1, status="sent", now=100)
    archive.add("max->tg", "ответ 100%", author="Борис", target_chat=2, status="sent", now=200)
    archive.add("http->max", "уведомление", target_chat=1, status="failed", error="x", now=300)
    archive.add("tg->max", "в очереди", target_chat=1, status="queued", now=400)


def test_list_newest_first_with_total(archive):
    fill(archive)
    rows, total = archive.list()
    assert total == 4
    assert [r["text"] for r in rows] == ["в очереди", "уведомление", "ответ 100%", "привет мир"]


def test_filters_by_direction_and_status(archive):
    fill(archive)
    rows, total = archive.list(direction="max->tg")
    assert total == 1 and rows[0]["author"] == "Борис"
    rows, total = archive.list(status="failed")
    assert total == 1 and rows[0]["error"] == "x"
    rows, total = archive.list(direction="tg->max", status="sent")
    assert total == 1


def test_unknown_filter_values_are_ignored(archive):
    fill(archive)
    _, total = archive.list(direction="'; DROP TABLE messages;--", status="bogus")
    assert total == 4


def test_search_in_text_and_author_with_wildcards_escaped(archive):
    fill(archive)
    assert archive.list(query="привет")[1] == 1
    assert archive.list(query="анна")[1] == 0 or archive.list(query="Анна")[1] == 1
    assert archive.list(query="100%")[1] == 1
    assert archive.list(query="%")[1] == 1  # только литеральный знак процента
    assert archive.list(query="_")[1] == 0  # подчёркивание не «любой символ»


def test_pagination(archive):
    for i in range(7):
        archive.add("tg->max", f"m{i}", status="sent", now=i)
    rows, total = archive.list(limit=3, offset=3)
    assert total == 7 and [r["text"] for r in rows] == ["m3", "m2", "m1"]


def test_purge_removes_only_old_finished_messages(archive):
    day = 86400
    archive.add("tg->max", "старое отправленное", status="sent", now=0)
    archive.add("tg->max", "старое проваленное", status="failed", now=0)
    archive.add("tg->max", "старое в очереди", status="queued", now=0)
    archive.add("tg->max", "свежее", status="sent", now=40 * day)
    removed = archive.purge(now=40 * day)
    assert removed == 2
    texts = {r["text"] for r in archive.list()[0]}
    assert texts == {"старое в очереди", "свежее"}


def test_zero_retention_keeps_everything(tmp_path):
    a = Archive(str(tmp_path / "a.db"), retention_days=0)
    a.add("tg->max", "x", status="sent", now=0)
    assert a.purge(now=10**9) == 0
    a.close()


def test_reader_does_not_create_missing_file(tmp_path):
    path = str(tmp_path / "nope.db")
    assert query_messages(path) == ([], 0)
    assert not os.path.exists(path)


@pytest.mark.skipif(sys.platform == "win32", reason="права файлов POSIX")
def test_database_file_is_private(archive):
    assert stat.S_IMODE(os.stat(archive.path).st_mode) == 0o600
