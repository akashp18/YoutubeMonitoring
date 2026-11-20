"""YouTubeBrowserMonitor: one clean, unauthenticated viewer visit per Short."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain import Availability, PlaybackStatus, RestrictionType
from app.services.page_scripts import PROBE_PAGE
from app.services.playback_verification import PlaybackOutcome, PlaybackVerificationService
from app.services.restriction_detection import RestrictionDetectionService, Verdict
from app.timeutil import iso

logger = logging.getLogger(__name__)

RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "err_name_not_resolved",
    "err_internet_disconnected",
    "err_connection",
    "err_network",
    "err_timed_out",
    "err_socket",
    "err_aborted",
    "err_empty_response",
    "err_http2",
    "target closed",
    "navigation failed",
)


@dataclass
class MonitorOutcome:
    availability: str
    playback_status: str
    restriction_type: str
    diagnostic_message: str
    playback_verified: bool = False
    playback_start_ms: int | None = None
    playback_checked_ms: int | None = None
    playback_completed: bool = False
    video_duration_seconds: float | None = None
    page_load_ms: int | None = None
    ready_state: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    attempts: int = 1
    http_status: int | None = None
    network_error: bool = False
    screenshot_path: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=iso)
    duration_ms: int = 0


class YouTubeBrowserMonitor:
    def __init__(
        self,
        browser_manager: Any,
        detection: RestrictionDetectionService,
        playback: PlaybackVerificationService | None = None,
        page_timeout_ms: int = 25000,
        max_retries: int = 1,
        settle_timeout_ms: int = 12000,
        screenshot_on_failure: bool = True,
        evidence_dir: Path | None = None,
    ) -> None:
        self.browser = browser_manager
        self.detection = detection
        self.playback = playback or PlaybackVerificationService()
        self.page_timeout_ms = max(5000, int(page_timeout_ms))
        self.max_retries = max(0, min(3, int(max_retries)))
        self.settle_timeout_ms = max(2000, int(settle_timeout_ms))
        self.screenshot_on_failure = screenshot_on_failure
        self.evidence_dir = Path(evidence_dir or Path("evidence"))

    async def check_video(
        self,
        video_id: str,
        run_id: int | None = None,
        capture_screenshot: bool = True,
        playback_seconds: float | None = None,
        verify_full: bool | None = None,
    ) -> MonitorOutcome:
        started = time.monotonic()
        attempts = 0
        last: MonitorOutcome | None = None
        playback_service = self.playback
        if playback_seconds is not None:
            playback_service = PlaybackVerificationService(
                start_timeout_ms=self.playback.start_timeout_ms,
                stall_timeout_ms=self.playback.stall_timeout_ms,
                verify_seconds=playback_seconds,
                mute=self.playback.mute,
                verify_full=(
                    self.playback.verify_full if verify_full is None else verify_full
                ),
                full_grace_ms=self.playback.full_grace_ms,
                max_full_playback_seconds=self.playback.max_full_playback_seconds,
                coverage_min=self.playback.coverage_min,
                coverage_max=self.playback.coverage_max,
            )

        for attempt in range(self.max_retries + 1):
            attempts += 1
            try:
                page = await self.browser.new_page()
            except Exception as exc:  # noqa: BLE001
                return MonitorOutcome(
                    availability=Availability.INCONCLUSIVE,
                    playback_status=PlaybackStatus.SKIPPED,
                    restriction_type=RestrictionType.UNKNOWN,
                    diagnostic_message=(
                        "The monitoring browser could not be started, so this video could not be "
                        f"checked: {exc}"
                    ),
                    attempts=attempts,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            try:
                outcome = await self._check_once(
                    page, video_id, run_id, capture_screenshot, playback_service
                )
            except Exception as exc:  # noqa: BLE001 - a check must never crash a run
                logger.exception("Unhandled monitor error for %s", video_id)
                outcome = MonitorOutcome(
                    availability=Availability.FAILED,
                    playback_status=PlaybackStatus.FAILED,
                    restriction_type=RestrictionType.UNKNOWN,
                    diagnostic_message=f"The browser check failed unexpectedly: {exc}",
                    attempts=attempts,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

            outcome.attempts = attempts
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            last = outcome
            retryable = outcome.availability == Availability.FAILED and outcome.network_error
            if retryable and attempt < self.max_retries:
                await asyncio.sleep(2.0 + attempt * 2.0)
                continue
            return outcome
        assert last is not None
        return last

    async def _check_once(
        self,
        page: Any,
        video_id: str,
        run_id: int | None,
        capture_screenshot: bool,
        playback_service: PlaybackVerificationService,
    ) -> MonitorOutcome:
        url = f"https://www.youtube.com/shorts/{video_id}?hl=en"
        http_status: int | None = None
        try:
            http_status = await page.goto(url, self.page_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - navigation failures are classified
            message = str(exc)
            retryable = any(marker in message.lower() for marker in RETRYABLE_ERROR_MARKERS)
            friendly = _friendly_navigation_error(message)
            return MonitorOutcome(
                availability=Availability.FAILED,
                playback_status=PlaybackStatus.SKIPPED,
                restriction_type=RestrictionType.UNKNOWN,
                diagnostic_message=f"{friendly} The page could not be loaded, so the video's "
                "availability could not be determined from the DOM.",
                http_status=None,
                network_error=True,
                evidence={
                    "console_errors": [],
                    "page_errors": [message[:500]],
                    "failed_requests": [{"url": url, "error": message[:200]}],
                    "page_signals": {"navigationError": message[:500], "retryable": retryable},
                    "page_url": url,
                },
            )

        page_started = time.monotonic()
        if http_status is not None and http_status >= 400:
            if http_status == 404:
                return MonitorOutcome(
                    availability=Availability.RESTRICTED,
                    playback_status=PlaybackStatus.SKIPPED,
                    restriction_type=RestrictionType.UNAVAILABLE,
                    diagnostic_message="YouTube returned HTTP 404 for this Short: it is no longer "
                    "available at that URL.",
                    http_status=http_status,
                )
            if http_status in {401, 403}:
                return MonitorOutcome(
                    availability=Availability.INCONCLUSIVE,
                    playback_status=PlaybackStatus.SKIPPED,
                    restriction_type=RestrictionType.CAPTCHA,
                    diagnostic_message=(
                        f"YouTube refused automated access with HTTP {http_status}. The check was "
                        "not completed and no attempt was made to bypass the block."
                    ),
                    http_status=http_status,
                )
            if http_status == 429:
                return MonitorOutcome(
                    availability=Availability.INCONCLUSIVE,
                    playback_status=PlaybackStatus.SKIPPED,
                    restriction_type=RestrictionType.UNKNOWN,
                    diagnostic_message="YouTube rate-limited the request (HTTP 429). The check was "
                    "deferred instead of retried aggressively.",
                    http_status=http_status,
                )
            return MonitorOutcome(
                availability=Availability.FAILED,
                playback_status=PlaybackStatus.SKIPPED,
                restriction_type=RestrictionType.UNKNOWN,
                diagnostic_message=f"YouTube returned HTTP {http_status}; the page could not be "
                "checked.",
                http_status=http_status,
                network_error=True,
            )

        signals = await self._wait_for_settle(page)
        page_load_ms = int((time.monotonic() - page_started) * 1000)
        signals["http_status"] = http_status
        verdict = self.detection.classify(signals, http_status=http_status)

        if verdict.verdict == "ACCESSIBLE":
            playback = await playback_service.verify(page)
            verdict, playback = await self._reconcile_after_playback(
                page, verdict, playback, playback_service
            )
            outcome = self._compose_accessible(verdict, playback, signals, page_load_ms, http_status)
        else:
            outcome = MonitorOutcome(
                availability=(
                    Availability.RESTRICTED
                    if verdict.verdict == "RESTRICTED"
                    else Availability.FAILED
                    if verdict.verdict == "FAILED"
                    else Availability.INCONCLUSIVE
                ),
                playback_status=PlaybackStatus.SKIPPED,
                restriction_type=verdict.restriction_type,
                diagnostic_message=verdict.message,
                page_load_ms=page_load_ms,
                http_status=http_status,
            )

        outcome.evidence = self._collect_evidence(page, signals, http_status, verdict)
        if (
            capture_screenshot
            and self.screenshot_on_failure
            and outcome.availability != Availability.PASS
        ):
            outcome.screenshot_path = await self._screenshot(page, video_id, run_id)
        return outcome

    async def _reconcile_after_playback(
        self,
        page: Any,
        verdict: Verdict,
        playback: PlaybackOutcome,
        playback_service: PlaybackVerificationService,
    ) -> tuple[Verdict, PlaybackOutcome]:
        if playback.status != PlaybackStatus.FAILED:
            return verdict, playback
        try:
            fresh = await page.evaluate(PROBE_PAGE)
        except Exception:  # noqa: BLE001
            return verdict, playback
        if not isinstance(fresh, dict):
            return verdict, playback
        refresh = self.detection.classify(fresh)
        if refresh.verdict in {"RESTRICTED", "INCONCLUSIVE"}:
            return refresh, playback
        return verdict, playback

    def _compose_accessible(
        self,
        verdict: Verdict,
        playback: PlaybackOutcome,
        signals: dict[str, Any],
        page_load_ms: int,
        http_status: int | None,
    ) -> MonitorOutcome:
        video = signals.get("video") or {}
        if playback.status == PlaybackStatus.VERIFIED:
            availability = Availability.PASS
            restriction = RestrictionType.NONE
        elif playback.status == PlaybackStatus.FAILED:
            availability = Availability.FAILED
            restriction = RestrictionType.PLAYBACK_ERROR
        else:
            availability = Availability.WARNING
            restriction = RestrictionType.NONE
        message = playback.message or verdict.message
        if availability == Availability.PASS:
            message = f"Publicly accessible. {playback.message}"
        return MonitorOutcome(
            availability=availability,
            playback_status=playback.status,
            restriction_type=restriction,
            diagnostic_message=message,
            playback_verified=playback.verified,
            playback_start_ms=playback.start_time_ms,
            playback_checked_ms=playback.checked_ms,
            playback_completed=playback.completed,
            video_duration_seconds=playback.duration_seconds,
            page_load_ms=page_load_ms,
            ready_state=playback.ready_state if playback.ready_state is not None else video.get("readyState"),
            video_width=video.get("videoWidth"),
            video_height=video.get("videoHeight"),
            http_status=http_status,
        )

    async def _wait_for_settle(self, page: Any) -> dict[str, Any]:
        deadline = time.monotonic() + self.settle_timeout_ms / 1000
        signals: dict[str, Any] = {}
        while True:
            try:
                signals = await page.evaluate(PROBE_PAGE)
            except Exception:  # noqa: BLE001
                signals = {}
            if isinstance(signals, dict) and self._is_settled(signals):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.35)
        if not isinstance(signals, dict):
            signals = {}
        return signals

    @staticmethod
    def _is_settled(signals: dict[str, Any]) -> bool:
        video = signals.get("video") or {}
        if video.get("present"):
            if int(video.get("readyState") or 0) >= 2:
                return True
            if int(video.get("readyState") or 0) >= 1:
                return True
        if signals.get("errorTexts"):
            return True
        if signals.get("hasChallenge") or signals.get("consentWall"):
            return True
        playability = signals.get("playability") or {}
        status = str(playability.get("status") or "").upper()
        if status and status != "OK":
            return True
        return False

    def _collect_evidence(
        self,
        page: Any,
        signals: dict[str, Any],
        http_status: int | None,
        verdict: Verdict,
    ) -> dict[str, Any]:
        mon = signals.get("mon") or {}
        extra: dict[str, Any] = {}
        if hasattr(page, "extra_evidence"):
            try:
                extra = page.extra_evidence() or {}
            except Exception:  # noqa: BLE001
                extra = {}
        console = list(mon.get("consoleErrors") or []) + list(extra.get("console_errors") or [])
        page_errors = list(mon.get("pageErrors") or []) + list(extra.get("page_errors") or [])
        return {
            "console_errors": console[:30],
            "page_errors": page_errors[:30],
            "failed_requests": list(extra.get("failed_requests") or [])[:30],
            "browser_version": extra.get("browser_version"),
            "page_url": signals.get("url"),
            "video_state": signals.get("video"),
            "page_signals": {
                "title": signals.get("title"),
                "playability": signals.get("playability"),
                "errorTexts": signals.get("errorTexts"),
                "hasChallenge": signals.get("hasChallenge"),
                "consentWall": signals.get("consentWall"),
                "videoCount": signals.get("videoCount"),
                "httpStatus": http_status,
                "verdict": {
                    "verdict": verdict.verdict,
                    "restriction": verdict.restriction_type,
                    "confidence": verdict.confidence,
                    "matched": verdict.matched,
                },
                "bodyExcerpt": str(signals.get("bodyText") or "")[:1500],
            },
        }

    async def _screenshot(self, page: Any, video_id: str, run_id: int | None) -> str | None:
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time())
            filename = f"{video_id}-{run_id or 'manual'}-{stamp}.png"
            path = self.evidence_dir / filename
            await page.screenshot(str(path))
            if path.exists() and path.stat().st_size > 0:
                return str(path)
            return None
        except Exception:  # noqa: BLE001 - screenshots are best effort
            logger.debug("Screenshot capture failed for %s", video_id, exc_info=True)
            return None


def _friendly_navigation_error(message: str) -> str:
    lowered = message.lower()
    if "timeout" in lowered:
        return "The page did not load before the navigation timeout."
    if "err_name_not_resolved" in lowered:
        return "YouTube could not be resolved (DNS failure); this looks like a network problem."
    if "err_internet_disconnected" in lowered or "err_network" in lowered:
        return "The network connection was lost while loading the page."
    if "err_connection" in lowered:
        return "The connection to YouTube failed."
    return f"Navigation failed: {message[:300]}"
