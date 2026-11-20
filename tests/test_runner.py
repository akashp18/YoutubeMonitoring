from __future__ import annotations

from pathlib import Path

import pytest

from app.domain import AlertType, Availability, RunStatus
from app.services.change_detection import StatusChangeDetector
from app.services.history import MonitoringHistoryService
from app.services.notifications import NotificationService
from app.services.runner import MonitoringRunner
from app.services.shorts_discovery import DiscoveryError, DiscoveryResult
from app.timeutil import parse_iso, utc_now
from tests.fakes import (
    FakeDiscoveryService,
    FakeResolver,
    QueueMonitor,
    pass_outcome,
    restricted_outcome,
)

ITEM_A = {"video_id": "aaaaaaaaaaa", "title": "Short A"}
ITEM_B = {"video_id": "bbbbbbbbbbb", "title": "Short B"}


def build_runner(repo, store, app_settings, discovery, monitor) -> MonitoringRunner:
    return MonitoringRunner(
        repo=repo,
        resolver=FakeResolver(),
        discovery=discovery,
        history=MonitoringHistoryService(repo, app_settings.evidence_dir),
        detector=StatusChangeDetector(),
        notifications=NotificationService(repo, store, app_settings),
        settings_store=store,
        browser_manager=None,
        evidence_dir=Path(app_settings.evidence_dir),
        monitor_factory=lambda: monitor,
    )


async def run_once(repo, runner, channel, scope: str = "channel", target_short_id=None):
    run = repo.runs.create(
        channel["id"], "manual", scope=scope, target_short_id=target_short_id
    )
    await runner.execute(run["id"])
    return repo.runs.get(run["id"])


async def test_run_discovers_checks_and_records_results(repo, channel, store, app_settings) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A, ITEM_B], source="browser")])
    monitor = QueueMonitor(
        {"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [restricted_outcome()]}
    )
    runner = build_runner(repo, store, app_settings, discovery, monitor)
    run = await run_once(repo, runner, channel)

    assert run["status"] == RunStatus.COMPLETED
    assert run["discovery_new_count"] == 2
    assert run["total_checked"] == 2
    assert run["passed_count"] == 1
    assert run["restricted_count"] == 1
    assert run["started_at"] and run["completed_at"]

    short_a = repo.shorts.get_by_video_id("aaaaaaaaaaa", channel["id"])
    short_b = repo.shorts.get_by_video_id("bbbbbbbbbbb", channel["id"])
    assert short_a["last_availability_status"] == Availability.PASS
    assert short_b["last_availability_status"] == Availability.RESTRICTED
    assert short_b["last_restriction_type"] == "UNAVAILABLE"

    alerts = repo.alerts.list(state="all")["items"]
    assert sum(1 for alert in alerts if alert["alert_type"] == AlertType.NEW_SHORT) == 2
    logs = repo.run_logs.list(run["id"])
    assert any("Discovery via browser" in log["message"] for log in logs)

    refreshed = repo.channels.get(channel["id"])
    assert refreshed["consecutive_failures"] == 0
    assert refreshed["next_run_at"] is not None


async def test_continuous_mode_queues_the_next_cycle_immediately(
    repo, channel, store, app_settings
) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A, ITEM_B])])
    runner = build_runner(
        repo,
        store,
        app_settings,
        discovery,
        QueueMonitor({"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [pass_outcome()]}),
    )
    run = await run_once(repo, runner, channel)
    assert run["status"] == RunStatus.COMPLETED

    refreshed = repo.channels.get(channel["id"])
    next_run = parse_iso(refreshed["next_run_at"])
    assert next_run is not None
    # Continuous mode schedules the next cycle seconds later instead of after
    # the channel's hourly interval.
    assert 0 <= (next_run - utc_now()).total_seconds() <= 20


async def test_single_short_recheck_does_not_disturb_the_channel_schedule(
    repo, channel, store, app_settings
) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A])])
    runner = build_runner(
        repo, store, app_settings, discovery, QueueMonitor({"aaaaaaaaaaa": [pass_outcome()]})
    )
    await run_once(repo, runner, channel)
    before = repo.channels.get(channel["id"])["next_run_at"]
    short = repo.shorts.get_by_video_id("aaaaaaaaaaa", channel["id"])
    assert short is not None
    await run_once(
        repo,
        runner,
        channel,
        scope="video",
        target_short_id=short["id"],
    )
    after = repo.channels.get(channel["id"])["next_run_at"]
    assert after == before


async def test_recovery_is_detected_and_incremental_checks_limit_work(
    repo, channel, store, app_settings
) -> None:
    # Incremental filtering applies to interval-based scheduling; continuous
    # cycles deliberately cover every Short each time.
    store.set_many({"monitoring.continuous_cycles": False})
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A, ITEM_B])])
    runner = build_runner(
        repo,
        store,
        app_settings,
        discovery,
        QueueMonitor({"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [restricted_outcome()]}),
    )
    await run_once(repo, runner, channel)

    monitor2 = QueueMonitor(
        {"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [pass_outcome()]}
    )
    runner2 = build_runner(repo, store, app_settings, discovery, monitor2)
    run2 = await run_once(repo, runner2, channel)
    # Short A is PASS and fresh, so only the failing Short is re-checked.
    assert monitor2.calls == ["bbbbbbbbbbb"]
    assert run2["total_checked"] == 1
    assert run2["passed_count"] == 1

    alerts = repo.alerts.list(state="all")["items"]
    assert any(alert["alert_type"] == AlertType.RECOVERED for alert in alerts)
    assert sum(1 for alert in alerts if alert["alert_type"] == AlertType.NEW_SHORT) == 2


async def test_disappeared_short_is_marked_delisted(repo, channel, store, app_settings) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A, ITEM_B])])
    runner = build_runner(
        repo,
        store,
        app_settings,
        discovery,
        QueueMonitor({"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [pass_outcome()]}),
    )
    await run_once(repo, runner, channel)

    discovery2 = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A])])
    runner2 = build_runner(
        repo, store, app_settings, discovery2, QueueMonitor([pass_outcome()])
    )
    run2 = await run_once(repo, runner2, channel)
    assert run2["discovery_removed_count"] == 1
    short_b = repo.shorts.get_by_video_id("bbbbbbbbbbb", channel["id"])
    assert short_b["is_listed"] == 0
    assert short_b["delisted_at"] is not None
    alerts = repo.alerts.list(state="all")["items"]
    assert any(alert["alert_type"] == AlertType.DELISTED for alert in alerts)


async def test_truncated_discovery_does_not_delist(repo, channel, store, app_settings) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A, ITEM_B])])
    runner = build_runner(
        repo,
        store,
        app_settings,
        discovery,
        QueueMonitor({"aaaaaaaaaaa": [pass_outcome()], "bbbbbbbbbbb": [pass_outcome()]}),
    )
    await run_once(repo, runner, channel)

    discovery2 = FakeDiscoveryService(
        [DiscoveryResult(items=[ITEM_A], truncated=True, source="browser")]
    )
    runner2 = build_runner(repo, store, app_settings, discovery2, QueueMonitor([pass_outcome()]))
    run2 = await run_once(repo, runner2, channel)
    assert run2["discovery_removed_count"] == 0
    assert repo.shorts.get_by_video_id("bbbbbbbbbbb", channel["id"])["is_listed"] == 1


async def test_discovery_failure_marks_run_failed_and_alerts(repo, channel, store, app_settings) -> None:
    class FailingDiscovery:
        max_scrolls = 0
        scroll_delay_ms = 0
        page_timeout_ms = 0

        async def discover(self, _channel):
            raise DiscoveryError("YouTube refused the channel page", "blocked")

    store.set_many({"notifications.run_failure_threshold": 1})
    runner = build_runner(
        repo, store, app_settings, FailingDiscovery(), QueueMonitor([pass_outcome()])
    )
    run = await run_once(repo, runner, channel)
    assert run["status"] == RunStatus.FAILED
    assert "discovery failed" in (run["error_message"] or "").lower()
    assert repo.channels.get(channel["id"])["consecutive_failures"] == 1
    alerts = repo.alerts.list(state="all")["items"]
    assert any(alert["alert_type"] == AlertType.RUN_FAILURES for alert in alerts)


async def test_single_video_recheck_skips_discovery(repo, channel, store, app_settings) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A])])
    runner = build_runner(repo, store, app_settings, discovery, QueueMonitor([pass_outcome()]))
    await run_once(repo, runner, channel)
    short = repo.shorts.get_by_video_id("aaaaaaaaaaa", channel["id"])
    discovery_calls = discovery.calls

    run = await run_once(
        repo, runner, channel, scope="video", target_short_id=short["id"]
    )
    assert discovery.calls == discovery_calls
    assert run["total_checked"] == 1
    assert repo.results.history(short["id"])[0]["monitoring_run_id"] == run["id"]


async def test_missing_channel_fails_cleanly(repo, store, app_settings) -> None:
    discovery = FakeDiscoveryService([DiscoveryResult(items=[ITEM_A])])
    runner = build_runner(repo, store, app_settings, discovery, QueueMonitor([pass_outcome()]))
    run = repo.runs.create(None, "manual")
    await runner.execute(run["id"])
    assert repo.runs.get(run["id"])["status"] == RunStatus.FAILED
