from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.domain import RunStatus
from app.services.scheduler import AlreadyRunningError, MonitoringScheduler
from app.timeutil import iso, utc_now


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.gate: asyncio.Event | None = None

    async def execute(self, run_id: int) -> None:
        self.calls.append(run_id)
        if self.gate is not None:
            await self.gate.wait()


def make_channel(
    repo,
    due: bool = True,
    name: str = "Example Channel",
    youtube_id: str = "UCabcdefghijklmnopqrstuv",
):
    return repo.channels.create(
        {
            "channel_url": "https://www.youtube.com/@examplechannel",
            "channel_id": youtube_id,
            "channel_name": name,
            "shorts_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/shorts",
            "monitoring_enabled": 1,
            "monitoring_interval_minutes": 60,
            "resolution_status": "resolved",
            "next_run_at": iso(utc_now() - timedelta(minutes=1)) if due else iso(utc_now() + timedelta(hours=1)),
        }
    )


async def test_due_channel_creates_and_runs_a_job(repo, store) -> None:
    channel = make_channel(repo, due=True)
    runner = RecordingRunner()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    await scheduler._tick()
    assert scheduler._run_task is not None
    await scheduler._run_task
    assert len(runner.calls) == 1
    run = repo.runs.get(runner.calls[0])
    assert run["channel_id"] == channel["id"]
    assert run["trigger_type"] == "schedule"
    assert repo.channels.get(channel["id"])["next_run_at"] is None


async def test_paused_scheduler_does_not_start_jobs(repo, store) -> None:
    make_channel(repo, due=True)
    store.set_many({"scheduler.paused": True})
    runner = RecordingRunner()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    await scheduler._tick()
    assert runner.calls == []
    assert scheduler.status()["paused"] is True


async def test_overlapping_jobs_are_prevented(repo, store) -> None:
    make_channel(repo, due=True)
    runner = RecordingRunner()
    runner.gate = asyncio.Event()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    await scheduler._tick()
    await asyncio.sleep(0)
    await scheduler._tick()
    await asyncio.sleep(0)
    await scheduler._tick()
    await asyncio.sleep(0)
    assert len(runner.calls) == 1
    assert repo.db.scalar("SELECT COUNT(*) FROM monitoring_runs") == 1
    runner.gate.set()
    await scheduler._run_task


async def test_manual_run_rejects_duplicate_channel_job(repo, store) -> None:
    channel = make_channel(repo, due=False)
    runner = RecordingRunner()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    await scheduler.request_run(channel["id"])
    await asyncio.sleep(0)
    assert len(runner.calls) == 1
    await scheduler._run_task
    repo.runs.create(channel["id"], "manual", status=RunStatus.QUEUED)
    with pytest.raises(AlreadyRunningError):
        await scheduler.request_run(channel["id"])


async def test_cancel_queued_run(repo, store) -> None:
    channel = make_channel(repo, due=False)
    store.set_many({"scheduler.paused": True})
    scheduler = MonitoringScheduler(repo, RecordingRunner(), store, tick_seconds=1)
    run = repo.runs.create(channel["id"], "manual")
    assert await scheduler.cancel(run["id"]) is True
    assert repo.runs.get(run["id"])["status"] == RunStatus.CANCELLED
    assert await scheduler.cancel(run["id"]) is False


async def test_restart_recovery_marks_running_interrupted(repo, store) -> None:
    channel = make_channel(repo, due=False)
    run = repo.runs.create(channel["id"], "schedule", status=RunStatus.RUNNING)
    repo.runs.mark_running(run["id"])
    recovered = repo.runs.recover_after_restart()
    assert recovered == 1
    assert repo.runs.get(run["id"])["status"] == RunStatus.INTERRUPTED


async def test_start_reconciles_missing_next_run(repo, store) -> None:
    channel = repo.channels.create(
        {
            "channel_url": "https://www.youtube.com/@examplechannel",
            "channel_id": "UCabcdefghijklmnopqrstuv",
            "monitoring_enabled": 1,
            "monitoring_interval_minutes": 60,
            "resolution_status": "resolved",
        }
    )
    runner = RecordingRunner()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    await scheduler.start()
    try:
        refreshed = repo.channels.get(channel["id"])
        assert refreshed["next_run_at"] is not None
    finally:
        await scheduler.stop()


async def test_run_due_now_creates_runs_once(repo, store) -> None:
    make_channel(repo, due=True, name="A", youtube_id="UCaaaaaaaaaaaaaaaaaaaaaa")
    make_channel(repo, due=True, name="B", youtube_id="UCbbbbbbbbbbbbbbbbbbbbbb")
    runner = RecordingRunner()
    scheduler = MonitoringScheduler(repo, runner, store, tick_seconds=1)
    created = await scheduler.run_due_now()
    assert len(created) == 2
    await asyncio.sleep(0)
    await scheduler._run_task
    second = await scheduler.run_due_now()
    assert second == []


async def test_status_reports_queue_and_next_run(repo, store) -> None:
    channel = make_channel(repo, due=False)
    repo.runs.create(channel["id"], "manual", status=RunStatus.QUEUED)
    scheduler = MonitoringScheduler(repo, RecordingRunner(), store, tick_seconds=1)
    status = scheduler.status()
    assert status["queued_runs"] == 1
    assert status["next_due_at"] is not None
    assert status["paused"] is False
