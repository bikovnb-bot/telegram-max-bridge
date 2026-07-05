from unittest.mock import AsyncMock

import pytest

from bridge.main import HostMonitor


def make_monitor(**overrides):
    alerts = AsyncMock()
    defaults = dict(
        alerts=alerts,
        path="/tmp",
        disk_threshold_percent=90.0,
        mem_threshold_percent=90.0,
        interval_seconds=300,
    )
    defaults.update(overrides)
    return HostMonitor(**defaults), alerts


@pytest.mark.asyncio
async def test_disk_alert_sent_once_above_threshold(monkeypatch):
    monitor, alerts = make_monitor()
    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 95.0)
    monkeypatch.setattr(monitor, "mem_usage_percent", lambda: None)

    await monitor.check_once()
    await monitor.check_once()

    assert alerts.notify.await_count == 1


@pytest.mark.asyncio
async def test_disk_alert_resends_after_recovery(monkeypatch):
    monitor, alerts = make_monitor()
    monkeypatch.setattr(monitor, "mem_usage_percent", lambda: None)

    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 95.0)
    await monitor.check_once()

    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 50.0)
    await monitor.check_once()

    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 95.0)
    await monitor.check_once()

    assert alerts.notify.await_count == 2


@pytest.mark.asyncio
async def test_mem_alert_independent_of_disk(monkeypatch):
    monitor, alerts = make_monitor()
    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 10.0)
    monkeypatch.setattr(monitor, "mem_usage_percent", lambda: 95.0)

    await monitor.check_once()

    assert alerts.notify.await_count == 1
    assert "Память" in alerts.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_no_alert_below_threshold(monkeypatch):
    monitor, alerts = make_monitor()
    monkeypatch.setattr(monitor, "disk_usage_percent", lambda: 50.0)
    monkeypatch.setattr(monitor, "mem_usage_percent", lambda: 50.0)

    await monitor.check_once()

    alerts.notify.assert_not_awaited()


def test_disk_usage_percent_handles_missing_path():
    monitor, _ = make_monitor(path="/this/path/does/not/exist/at/all")
    assert monitor.disk_usage_percent() is None


def test_mem_usage_percent_returns_none_without_proc_meminfo(monkeypatch):
    monitor, _ = make_monitor()

    def fake_open(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("builtins.open", fake_open)
    assert monitor.mem_usage_percent() is None
