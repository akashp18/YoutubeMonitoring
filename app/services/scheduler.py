"""MonitoringScheduler: in-process scheduling for short-lived monitoring jobs.

The project had no scheduler, so this module provides one on top of the asyncio
event loop instead of adding a second scheduling system. Durable state lives in
SQLite (`next_run_at`, queued/running runs), which is what makes restarts safe:

* runs queued before a restart are executed by the next tick;
* runs that were RUNNING are marked INTERRUPTED, their partial results kept;
* `next_run_at` is recomputed for every enabled channel on startup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.domain import RunStatus, TriggerType
from app.repositories import Repository
from app.services.runner import MonitoringRunner
from app.settings_store import SettingsStore
from datetime import timedelta

from app.timeutil import add_minutes, iso, parse_iso, utc_now

logger = logging.getLogger(__name__)


class AlreadyRunningError(Exception):
    pass


class MonitoringScheduler:
    def __init__(
        self,
        repo: Repository,
        runner: MonitoringRunner,
        settings_store: SettingsStore,
        tick_seconds: float = 5.0,
    ) -> None:
        self.repo = repo
        self.runner = runner
        self.settings = settings_store
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._loop_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._active_run_id: int | None = None
        self._stopping = False
        self._last_tick_at: str | None = None

    async def start(self) -> None:
        recovered = self.repo.runs.recover_after_restart()
        if recovered:
            logger.info("Marked %s interrupted run(s) after restart", recovered)
        self._reconcile_schedule()
        self._stopping = False
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop(), name="monitoring-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
        self._run_task = None
        self._active_run_id = None

    def _reconcile_schedule(self) -> None:
        """Rebuild next_run_at after a restart from the persisted last run."""
        now = utc_now()
        for channel in self.repo.channels.scheduling_state():
            if not channel.get("monitoring_enabled") or not channel.get("monitoring_interval_minutes"):
                continue
            if channel.get("next_run_at"):
                continue
            interval = int(channel["monitoring_interval_minutes"])
            base = parse_iso(channel.get("last_run_at")) or (now - timedelta(minutes=interval))
            due = base + timedelta(minutes=interval)
            if due <= now:
                due = now + timedelta(seconds=20 + (int(channel["id"]) % 5) * 10)
            self.repo.channels.set_next_run(channel["id"], iso(due))

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive any tick failure
                logger.exception("Scheduler tick failed")
            self._last_tick_at = iso()
            try:
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if self._stopping:
            return
        if self.settings.get_bool("scheduler.paused", False):
            return
        if self._run_task is not None and not self._run_task.done():
            return

        run = self.repo.runs.next_queued()
        if run is None:
            for channel in self.repo.channels.due():
                if self.repo.runs.has_open_run(channel["id"]):
                    interval = int(channel.get("monitoring_interval_minutes") or 60)
                    self.repo.channels.set_next_run(channel["id"], add_minutes(interval))
                    continue
                run = self.repo.runs.create(channel["id"], TriggerType.SCHEDULE)
                # Cleared until the run finishes so the channel is not due again.
                self.repo.channels.set_next_run(channel["id"], None)
                self.repo.run_logs.add(
                    run["id"], f"Scheduled run created for channel {channel.get('channel_name') or channel['id']}."
                )
                break
        if run is not None:
            self._launch(run["id"])

    def _launch(self, run_id: int) -> None:
        self._active_run_id = run_id
        self._run_task = asyncio.create_task(self._execute(run_id), name=f"monitor-run-{run_id}")

    async def _execute(self, run_id: int) -> None:
        try:
            await self.runner.execute(run_id)
        except Exception:  # noqa: BLE001 - runner already records failures
            logger.exception("Run %s crashed", run_id)
        finally:
            self._active_run_id = None

    async def kick(self) -> None:
        """Start the next queued run if the scheduler is idle."""
        if self._stopping or self.settings.get_bool("scheduler.paused", False):
            return
        if self._run_task is not None and not self._run_task.done():
            return
        run = self.repo.runs.next_queued()
        if run is not None:
            self._launch(run["id"])

    async def request_run(
        self,
        channel_id: int,
        scope: str = "channel",
        target_short_id: int | None = None,
        trigger: str = TriggerType.MANUAL,
    ) -> dict[str, Any]:
        if scope == "channel" and self.repo.runs.has_open_run(channel_id):
            raise AlreadyRunningError("A monitoring run for this channel is already queued or running.")
        if target_short_id is not None:
            open_target = self.repo.db.query_one(
                "SELECT id FROM monitoring_runs WHERE target_short_id = ? "
                "AND status IN ('QUEUED', 'RUNNING') LIMIT 1",
                (target_short_id,),
            )
            if open_target:
                raise AlreadyRunningError("A check for this Short is already queued or running.")
        run = self.repo.runs.create(channel_id, trigger, scope=scope, target_short_id=target_short_id)
        self.repo.run_logs.add(run["id"], f"Manual run queued ({scope}).")
        await self.kick()
        return self.repo.runs.get(run["id"]) or run

    async def cancel(self, run_id: int) -> bool:
        accepted = self.repo.runs.request_cancel(run_id)
        if accepted and self._active_run_id == run_id and self._run_task is not None:
            if not self._run_task.done():
                self._run_task.cancel()
        return accepted

    def set_paused(self, paused: bool) -> dict[str, Any]:
        self.settings.set_many({"scheduler.paused": bool(paused)})
        logger.info("Monitoring scheduler %s", "paused" if paused else "resumed")
        return self.status()

    def status(self) -> dict[str, Any]:
        next_due = self.repo.db.scalar(
            "SELECT MIN(next_run_at) FROM youtube_channels WHERE monitoring_enabled = 1 "
            "AND monitoring_interval_minutes > 0 AND next_run_at IS NOT NULL"
        )
        return {
            "paused": self.settings.get_bool("scheduler.paused", False),
            "active_run": self.repo.runs.active(),
            "next_due_at": next_due,
            "queued_runs": int(
                self.repo.db.scalar("SELECT COUNT(*) FROM monitoring_runs WHERE status = 'QUEUED'")
                or 0
            ),
            "loop_running": bool(self._loop_task and not self._loop_task.done()),
            "active_run_id": self._active_run_id,
            "tick_seconds": self.tick_seconds,
            "last_tick_at": self._last_tick_at,
            "server_time": iso(),
            "channels": self.repo.channels.scheduling_state(),
        }

    async def run_due_now(self) -> list[dict[str, Any]]:
        """Queue a run for every channel that is currently due (used by the UI)."""
        created: list[dict[str, Any]] = []
        for channel in self.repo.channels.due():
            if self.repo.runs.has_open_run(channel["id"]):
                continue
            run = self.repo.runs.create(channel["id"], TriggerType.MANUAL)
            created.append(run)
            self.repo.channels.set_next_run(channel["id"], None)
        if created:
            await self.kick()
        return created
