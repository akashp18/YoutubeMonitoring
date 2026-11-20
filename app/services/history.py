"""MonitoringHistoryService: persist results, evidence and short state."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.domain import Availability
from app.repositories import Repository
from app.services.browser_monitor import MonitorOutcome
from app.timeutil import iso, utc_now

logger = logging.getLogger(__name__)


class MonitoringHistoryService:
    def __init__(self, repo: Repository, evidence_dir: str | Path) -> None:
        self.repo = repo
        self.evidence_dir = Path(evidence_dir)

    def record(
        self,
        short: dict[str, Any],
        outcome: MonitorOutcome,
        run_id: int | None,
        retention_days: int = 30,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        previous = self.repo.results.latest_for_short(short["id"])
        checked_at = outcome.checked_at or iso()

        evidence_id = None
        if outcome.evidence or outcome.screenshot_path:
            evidence_id = self._store_evidence(short, outcome, run_id, retention_days)

        failure_marker = outcome.availability in (
            Availability.RESTRICTED,
            Availability.FAILED,
            Availability.INCONCLUSIVE,
        )
        result = self.repo.results.insert(
            {
                "short_id": short["id"],
                "video_id": short["video_id"],
                "monitoring_run_id": run_id,
                "availability_status": outcome.availability,
                "playback_status": outcome.playback_status,
                "restriction_type": outcome.restriction_type,
                "diagnostic_message": outcome.diagnostic_message,
                "playback_start_time_ms": outcome.playback_start_ms,
                "playback_verified": 1 if outcome.playback_verified else 0,
                "playback_completed": 1 if outcome.playback_completed else 0,
                "video_duration_seconds": outcome.video_duration_seconds,
                "playback_checked_ms": outcome.playback_checked_ms,
                "page_load_ms": outcome.page_load_ms,
                "ready_state": outcome.ready_state,
                "video_width": outcome.video_width,
                "video_height": outcome.video_height,
                "checked_at": checked_at,
                "check_duration_ms": outcome.duration_ms,
                "attempt_count": max(1, outcome.attempts),
                "evidence_id": evidence_id,
            }
        )

        if previous is None:
            status_changed = outcome.availability not in (Availability.PASS,)
        else:
            status_changed = (
                previous["availability_status"] != outcome.availability
                or previous["restriction_type"] != outcome.restriction_type
            )

        self.repo.shorts.update_after_check(
            short["id"],
            availability_status=outcome.availability,
            playback_status=outcome.playback_status,
            restriction_type=outcome.restriction_type,
            playback_start_ms=outcome.playback_start_ms,
            playback_completed=outcome.playback_completed,
            diagnostic_message=outcome.diagnostic_message,
            checked_at=checked_at,
            status_changed=status_changed,
            failed=failure_marker,
        )
        return result, previous

    def _store_evidence(
        self,
        short: dict[str, Any],
        outcome: MonitorOutcome,
        run_id: int | None,
        retention_days: int,
    ) -> int:
        from datetime import timedelta

        evidence = outcome.evidence or {}
        expires = iso(utc_now() + timedelta(days=max(1, retention_days)))
        screenshot_bytes = None
        if outcome.screenshot_path:
            try:
                screenshot_bytes = Path(outcome.screenshot_path).stat().st_size
            except OSError:
                screenshot_bytes = None
        return self.repo.evidence.insert(
            {
                "short_id": short["id"],
                "monitoring_run_id": run_id,
                "video_id": short["video_id"],
                "screenshot_path": outcome.screenshot_path,
                "console_errors": evidence.get("console_errors") or [],
                "page_errors": evidence.get("page_errors") or [],
                "failed_requests": evidence.get("failed_requests") or [],
                "video_state": evidence.get("video_state") or {},
                "page_signals": evidence.get("page_signals") or {},
                "browser_version": evidence.get("browser_version"),
                "page_url": evidence.get("page_url"),
                "captured_at": outcome.checked_at,
                "expires_at": expires,
                "screenshot_bytes": screenshot_bytes,
            }
        )

    def purge_expired_evidence(self) -> int:
        removed = 0
        for row in self.repo.evidence.expired():
            path = row.get("screenshot_path")
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not delete evidence file %s", path, exc_info=True)
            self.repo.evidence.delete(row["id"])
            removed += 1
        return removed
