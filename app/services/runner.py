"""MonitoringRunner: executes one monitoring run end to end."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from app.domain import (
    AlertType,
    Availability,
    PlaybackStatus,
    RestrictionType,
    RunStatus,
    TriggerType,
)
from app.repositories import Repository
from app.services.browser_monitor import YouTubeBrowserMonitor
from app.services.change_detection import ChangeEvent, StatusChangeDetector
from app.services.channel_resolver import ChannelResolutionError, ChannelResolver
from app.services.channel_url import ChannelUrlError, parse_channel_url
from app.services.history import MonitoringHistoryService
from app.services.notifications import NotificationService
from app.services.playback_verification import PlaybackVerificationService
from app.services.restriction_detection import RestrictionDetectionService
from app.services.shorts_discovery import DiscoveryError, DiscoveryResult, ShortsDiscoveryService
from app.settings_store import SettingsStore
from app.timeutil import add_minutes, iso

logger = logging.getLogger(__name__)


class RunSetupError(Exception):
    def __init__(self, message: str, code: str = "setup_failed") -> None:
        super().__init__(message)
        self.code = code


class MonitoringRunner:
    def __init__(
        self,
        repo: Repository,
        resolver: ChannelResolver,
        discovery: ShortsDiscoveryService,
        history: MonitoringHistoryService,
        detector: StatusChangeDetector,
        notifications: NotificationService,
        settings_store: SettingsStore,
        browser_manager: Any | None = None,
        evidence_dir: str | Path = "evidence",
        monitor_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.repo = repo
        self.resolver = resolver
        self.discovery = discovery
        self.history = history
        self.detector = detector
        self.notifications = notifications
        self.settings = settings_store
        self.browser_manager = browser_manager
        self.evidence_dir = Path(evidence_dir)
        self._monitor_factory = monitor_factory

    def build_monitor(self) -> Any:
        if self._monitor_factory is not None:
            return self._monitor_factory()
        if self.browser_manager is None:
            raise RunSetupError("No browser manager is configured.")
        page_timeout = self.settings.get_int("browser.page_timeout_ms", 25000)
        return YouTubeBrowserMonitor(
            browser_manager=self.browser_manager,
            detection=RestrictionDetectionService(),
            playback=PlaybackVerificationService(
                start_timeout_ms=self.settings.get_int("browser.playback_start_timeout_ms", 15000),
                stall_timeout_ms=self.settings.get_int("browser.stall_timeout_ms", 4000),
                mute=self.settings.get_bool("browser.mute_playback", True),
                verify_full=self.settings.get_bool("monitoring.verify_full_playback", False),
                coverage_min=self.settings.get_int("monitoring.playback_min_percent", 60) / 100,
                coverage_max=self.settings.get_int("monitoring.playback_max_percent", 100) / 100,
            ),
            page_timeout_ms=page_timeout,
            max_retries=self.settings.get_int("browser.max_retries", 1),
            settle_timeout_ms=min(12000, page_timeout),
            screenshot_on_failure=self.settings.get_bool("browser.screenshot_on_failure", True),
            evidence_dir=self.evidence_dir,
        )

    async def execute(self, run_id: int) -> None:
        run = self.repo.runs.get(run_id)
        if not run:
            return
        if run["status"] not in (RunStatus.QUEUED, RunStatus.RUNNING):
            return
        if run["status"] == RunStatus.QUEUED:
            self.repo.runs.mark_running(run_id)
        self._log(run_id, f"Run #{run_id} started (trigger={run['trigger_type']}, scope={run['scope']}).")
        channel = self.repo.channels.get(run["channel_id"]) if run["channel_id"] else None
        if channel is None:
            self.repo.runs.finalize(
                run_id, RunStatus.FAILED, "The channel configured for this run no longer exists."
            )
            return

        try:
            await self._execute_inner(run, channel)
        except asyncio.CancelledError:
            cancelled = self.repo.runs.cancel_requested(run_id)
            status = RunStatus.CANCELLED if cancelled else RunStatus.INTERRUPTED
            message = (
                "Run was cancelled by the user."
                if cancelled
                else "Run was interrupted by application shutdown."
            )
            self.repo.runs.finalize(run_id, status, message)
            self._log(run_id, message, "warning")
            self._update_channel_schedule(channel, success=False, scope=run["scope"])
            return
        except RunSetupError as exc:
            self.repo.runs.finalize(run_id, RunStatus.FAILED, str(exc))
            self._log(run_id, f"Run could not start: {exc}", "error")
            self._update_channel_schedule(channel, success=False, scope=run["scope"])
            await self._after_run(run_id, channel)
        except DiscoveryError as exc:
            self.repo.runs.finalize(run_id, RunStatus.FAILED, f"Shorts discovery failed: {exc}")
            self._log(run_id, f"Shorts discovery failed: {exc}", "error")
            self._update_channel_schedule(channel, success=False, scope=run["scope"])
            await self._after_run(run_id, channel)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop monitoring
            logger.exception("Monitoring run %s failed", run_id)
            self.repo.runs.finalize(run_id, RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
            self._log(run_id, f"Run failed: {type(exc).__name__}: {exc}", "error")
            self._update_channel_schedule(channel, success=False, scope=run["scope"])
            await self._after_run(run_id, channel)

    async def _execute_inner(self, run: dict[str, Any], channel: dict[str, Any]) -> None:
        run_id = run["id"]
        channel = await self._ensure_resolved(channel, run_id)
        started = time.monotonic()

        if run["scope"] == "channel":
            await self._discover(run_id, channel)

        channel = self.repo.channels.get(channel["id"]) or channel
        max_shorts = int(
            channel.get("max_shorts_per_run")
            or self.settings.get_int("monitoring.max_shorts_per_run", 40)
        )
        interval = int(channel.get("monitoring_interval_minutes") or 0)
        factor = self.settings.get_float("monitoring.pass_recheck_factor", 1.0)
        continuous = self.settings.get_bool("monitoring.continuous_cycles", True)
        include_pass = bool(channel.get("incremental_checks", 1))
        pass_cutoff = add_minutes(-max(15.0, (interval or 60) * factor))
        if continuous and run["scope"] == "channel":
            # A continuous cycle covers the whole listing every time instead of
            # waiting for the PASS re-check window.
            include_pass = True
            pass_cutoff = iso()
            self._log(
                run_id,
                "Continuous mode: every listed Short is eligible for this cycle.",
            )

        candidates = self.repo.shorts.candidates_for_check(
            channel_id=channel["id"],
            limit=max(1, min(500, max_shorts)),
            pass_cutoff=pass_cutoff,
            include_pass=include_pass,
            target_short_id=run.get("target_short_id"),
        )
        if self.settings.get_bool("monitoring.randomize_order", True) and len(candidates) > 1:
            random.shuffle(candidates)
            self._log(
                run_id,
                f"Check order randomized for {len(candidates)} Shorts (priority selection happens before shuffling).",
            )
        if not candidates:
            self._log(run_id, "No Shorts were due for a check in this run.")
            self.repo.runs.finalize(run_id, RunStatus.COMPLETED)
            self._update_channel_schedule(channel, success=True, scope=run["scope"])
            await self._after_run(run_id, channel)
            return

        monitor = self.build_monitor()
        concurrency = max(1, min(4, self.settings.get_int("browser.max_concurrent_checks", 2)))
        retention_days = self.settings.get_int("evidence.retention_days", 30)
        screenshot_limit = self.settings.get_int("evidence.max_screenshots_per_run", 25)
        screenshots_used = 0
        checked = 0
        semaphore = asyncio.Semaphore(concurrency)
        cancelled = False

        async def check_short(short: dict[str, Any]) -> None:
            nonlocal screenshots_used, checked
            if self.repo.runs.cancel_requested(run_id):
                return
            async with semaphore:
                if self.repo.runs.cancel_requested(run_id):
                    return
                capture = screenshots_used < screenshot_limit
                try:
                    outcome = await monitor.check_video(
                        short["video_id"],
                        run_id=run_id,
                        capture_screenshot=capture,
                        playback_seconds=float(
                            channel.get("playback_verification_seconds") or 5.0
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - never lose a check
                    logger.exception("Check crashed for %s", short["video_id"])
                    outcome = _synthetic_failure(short, exc)
                if outcome.screenshot_path:
                    screenshots_used += 1
                try:
                    result, previous = self.history.record(
                        short, outcome, run_id, retention_days=retention_days
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Could not persist result for %s", short["video_id"])
                    return
                checked += 1
                events = self.detector.detect(previous, result, short)
                for event in events:
                    self._create_alert(short, reason_event=event)
                if outcome.availability == Availability.PASS:
                    if not any(e.alert_type == AlertType.PLAYBACK_PERFORMANCE for e in events):
                        self.repo.alerts.resolve_by_key(
                            f"{short['id']}:{AlertType.PLAYBACK_PERFORMANCE}"
                        )
                    self.repo.alerts.resolve_for_short(short["id"])
                self._log(
                    run_id,
                    f"{short['video_id']} {outcome.availability} / {outcome.playback_status}"
                    + (f" - {outcome.diagnostic_message[:160]}" if outcome.diagnostic_message else ""),
                    "error" if outcome.availability in (Availability.FAILED,) else "info",
                )

        batch_size = concurrency
        for index in range(0, len(candidates), batch_size):
            if self.repo.runs.cancel_requested(run_id):
                cancelled = True
                break
            batch = candidates[index : index + batch_size]
            await asyncio.gather(*(check_short(short) for short in batch))

        status = (
            RunStatus.CANCELLED
            if cancelled or self.repo.runs.cancel_requested(run_id)
            else RunStatus.COMPLETED
        )
        duration = int(time.monotonic() - started)
        self.repo.runs.finalize(
            run_id,
            status,
            "Run cancelled by the user." if status == RunStatus.CANCELLED else None,
        )
        self._log(
            run_id,
            f"Run finished with status {status} after checking {checked} Shorts in {duration} ms.",
            "warning" if status == RunStatus.CANCELLED else "info",
        )
        self._update_channel_schedule(
            channel, success=status == RunStatus.COMPLETED, scope=run["scope"]
        )
        await self._after_run(run_id, channel)

    async def _discover(self, run_id: int, channel: dict[str, Any]) -> None:
        listed_before = int(
            self.repo.db.scalar(
                "SELECT COUNT(*) FROM youtube_shorts WHERE channel_id = ? AND is_listed = 1",
                (channel["id"],),
            )
            or 0
        )
        self.discovery.max_scrolls = self.settings.get_int("browser.discovery_max_scrolls", 12)
        self.discovery.scroll_delay_ms = self.settings.get_int(
            "browser.discovery_scroll_delay_ms", 900
        )
        self.discovery.page_timeout_ms = self.settings.get_int("browser.page_timeout_ms", 25000)
        result: DiscoveryResult = await self.discovery.discover(channel)
        self._log(
            run_id,
            f"Discovery via {result.source} found {len(result.items)} Shorts"
            + (" (truncated)" if result.truncated else ""),
        )
        for note in result.notes:
            self._log(run_id, f"Discovery note: {note}", "warning")

        new_rows, _all_rows = self.repo.shorts.upsert_discovered(channel["id"], result.items)
        discovered_ids = {item["video_id"] for item in result.items}
        delisted_rows: list[dict[str, Any]] = []
        suspicious = (
            listed_before >= 5 and len(discovered_ids) < max(3, listed_before * 0.5)
        )
        if result.truncated or suspicious:
            if suspicious:
                self._log(
                    run_id,
                    "Skipped disappearance detection: far fewer Shorts were found than previously "
                    "known, which usually means YouTube truncated the listing.",
                    "warning",
                )
        elif discovered_ids:
            delisted_rows = self.repo.shorts.mark_delisted(channel["id"], list(discovered_ids))
        else:
            self._log(
                run_id,
                "Discovery returned no Shorts; disappearance detection was skipped to avoid "
                "false alerts.",
                "warning",
            )

        self.repo.runs.set_discovery_summary(
            run_id,
            source=result.source,
            total=len(result.items),
            new_count=len(new_rows),
            removed_count=len(delisted_rows),
            truncated=result.truncated,
        )

        if new_rows:
            self._log(run_id, f"{len(new_rows)} new Short(s) discovered.")
        for row in new_rows:
            self._create_new_short_alert(row, channel)
        for row in delisted_rows:
            self._create_delisted_alert(row, channel)

    async def _ensure_resolved(self, channel: dict[str, Any], run_id: int) -> dict[str, Any]:
        if (
            channel.get("channel_id")
            and channel.get("resolution_status") == "resolved"
            and channel.get("shorts_url")
        ):
            return channel
        try:
            parsed = parse_channel_url(channel["channel_url"])
        except ChannelUrlError as exc:
            raise RunSetupError(f"The saved channel URL is invalid: {exc}") from exc
        try:
            resolved = await self.resolver.resolve(parsed)
        except ChannelResolutionError as exc:
            self.repo.channels.set_resolution(channel["id"], None, str(exc))
            raise RunSetupError(f"The channel could not be resolved: {exc}") from exc
        self.repo.channels.set_resolution(channel["id"], resolved)
        self._log(
            run_id,
            f"Resolved channel to {resolved.channel_id}"
            + (f" ({resolved.channel_name})" if resolved.channel_name else ""),
        )
        return self.repo.channels.get(channel["id"]) or channel

    def _create_alert(self, short: dict[str, Any], reason_event: ChangeEvent) -> None:
        alert_id = self.repo.alerts.create_if_absent(
            {
                "channel_id": short["channel_id"],
                "short_id": short["id"],
                "video_id": short["video_id"],
                "alert_type": reason_event.alert_type,
                "previous_status": reason_event.previous_status,
                "current_status": reason_event.current_status,
                "restriction_type": reason_event.restriction_type,
                "severity": reason_event.severity,
                "message": reason_event.message,
                "diagnostic_excerpt": reason_event.message[:400],
                "dedupe_key": self.detector.dedupe_key(short["id"], reason_event),
                "created_at": iso(),
            }
        )
        if alert_id:
            logger.info("Alert %s created: %s", alert_id, reason_event.alert_type)

    def _create_new_short_alert(self, short: dict[str, Any], channel: dict[str, Any]) -> None:
        notify_channel = bool(channel.get("notify_new_shorts", 1))
        notify_global = self.settings.get_bool("notifications.notify_on_new_shorts", True)
        if not (notify_channel and notify_global):
            return
        self.repo.alerts.create_if_absent(
            {
                "channel_id": short["channel_id"],
                "short_id": short["id"],
                "video_id": short["video_id"],
                "alert_type": AlertType.NEW_SHORT,
                "previous_status": None,
                "current_status": "NEW",
                "restriction_type": RestrictionType.NONE,
                "severity": "info",
                "message": f"New Short discovered: \u201c{short.get('title') or short['video_id']}\u201d "
                f"on {channel.get('channel_name') or channel.get('channel_id')}.",
                "diagnostic_excerpt": f"First seen at {short.get('first_discovered_at')}",
                "dedupe_key": f"{short['id']}:{AlertType.NEW_SHORT}",
                "created_at": iso(),
            },
            suppress_if_any=True,
        )

    def _create_delisted_alert(self, short: dict[str, Any], channel: dict[str, Any]) -> None:
        stamp = short.get("delisted_at") or iso()
        self.repo.alerts.create_if_absent(
            {
                "channel_id": short["channel_id"],
                "short_id": short["id"],
                "video_id": short["video_id"],
                "alert_type": AlertType.DELISTED,
                "previous_status": short.get("last_availability_status"),
                "current_status": "NOT_LISTED",
                "restriction_type": RestrictionType.NONE,
                "severity": "warning",
                "message": f"\u201c{short.get('title') or short['video_id']}\u201d is no longer listed "
                f"in the Shorts tab of {channel.get('channel_name') or channel.get('channel_id')}. "
                "It may have been deleted, made private or removed from the Shorts shelf.",
                "diagnostic_excerpt": f"Delisted at {stamp}",
                "dedupe_key": f"{short['id']}:{AlertType.DELISTED}:{stamp}",
                "created_at": iso(),
            },
            suppress_if_any=True,
        )

    def _update_channel_schedule(
        self, channel: dict[str, Any], success: bool, scope: str = "channel"
    ) -> None:
        # A single-Short re-check must not disturb the channel's own cycle.
        if scope != "channel":
            return
        interval = int(channel.get("monitoring_interval_minutes") or 0)
        continuous = self.settings.get_bool("monitoring.continuous_cycles", True)
        if continuous and success:
            # Chain cycles: the scheduler picks the channel up again on its next
            # tick once the short grace period has passed.
            next_run = add_minutes(10 / 60)
        else:
            next_run = add_minutes(interval) if interval > 0 else None
        self.repo.channels.set_next_run(channel["id"], next_run)
        self.repo.channels.mark_run_completed(channel["id"], success=success)

    async def _after_run(self, run_id: int, channel: dict[str, Any]) -> None:
        refreshed = self.repo.channels.get(channel["id"])
        threshold = self.settings.get_int("notifications.run_failure_threshold", 3)
        if refreshed and int(refreshed.get("consecutive_failures") or 0) >= threshold:
            self.repo.alerts.create_if_absent(
                {
                    "channel_id": refreshed["id"],
                    "short_id": None,
                    "video_id": None,
                    "alert_type": AlertType.RUN_FAILURES,
                    "previous_status": None,
                    "current_status": RunStatus.FAILED,
                    "restriction_type": RestrictionType.NONE,
                    "severity": "critical",
                    "message": (
                        f"{refreshed['consecutive_failures']} consecutive monitoring runs failed for "
                        f"{refreshed.get('channel_name') or refreshed.get('channel_id')}. "
                        "Check the run log for the underlying error."
                    ),
                    "diagnostic_excerpt": f"Run #{run_id} finished with a failure.",
                    "dedupe_key": f"channel:{refreshed['id']}:{AlertType.RUN_FAILURES}",
                    "created_at": iso(),
                }
            )
        if refreshed and int(refreshed.get("consecutive_failures") or 0) == 0:
            self.repo.alerts.resolve_by_key(f"channel:{refreshed['id']}:{AlertType.RUN_FAILURES}")
        try:
            await self.notifications.process_pending()
        except Exception:  # noqa: BLE001 - notification problems must not fail the run
            logger.exception("Notification delivery failed")
            self._log(run_id, "Notification delivery failed; alerts remain in the dashboard.", "warning")

    def _log(self, run_id: int, message: str, level: str = "info") -> None:
        try:
            self.repo.run_logs.add(run_id, message, level)
        except Exception:  # noqa: BLE001
            logger.debug("Could not write run log", exc_info=True)


def _synthetic_failure(short: dict[str, Any], exc: Exception) -> Any:
    from app.services.browser_monitor import MonitorOutcome

    return MonitorOutcome(
        availability=Availability.FAILED,
        playback_status=PlaybackStatus.FAILED,
        restriction_type=RestrictionType.UNKNOWN,
        diagnostic_message=f"The check could not be completed: {type(exc).__name__}: {exc}",
        evidence={
            "page_errors": [str(exc)[:500]],
            "page_signals": {"error": "monitor exception", "video_id": short.get("video_id")},
        },
    )


__all__ = ["MonitoringRunner", "RunSetupError", "TriggerType"]
