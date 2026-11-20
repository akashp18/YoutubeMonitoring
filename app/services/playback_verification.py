"""PlaybackVerificationService: prove that a Short actually plays.

In full mode (the default) each Short is played from the start to its final
frame and is only reported VERIFIED once the player reaches the end. Playback is
muted and never touches likes, comments, subscriptions or shares.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.domain import PlaybackStatus
from app.services.page_scripts import (
    CLEANUP_PLAYBACK,
    READ_PLAYBACK,
    REQUEST_PLAY,
    START_PLAYBACK_MONITOR,
    VIDEO_STATE,
)

MIN_ADVANCE_SECONDS = 0.3
READY_STATE_HAVE_CURRENT_DATA = 2
MAX_FULL_PLAYBACK_SECONDS = 200.0


@dataclass
class PlaybackOutcome:
    status: str
    verified: bool
    start_time_ms: int | None
    checked_ms: int
    message: str
    video_state: dict[str, Any] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    stalled: bool = False
    autoplay_blocked: bool = False
    advanced_seconds: float = 0.0
    ready_state: int | None = None
    completed: bool = False
    duration_seconds: float | None = None
    watched_seconds: float = 0.0


class PlaybackVerificationService:
    def __init__(
        self,
        start_timeout_ms: int = 15000,
        stall_timeout_ms: int = 4000,
        verify_seconds: float = 5.0,
        mute: bool = True,
        verify_full: bool = False,
        full_grace_ms: int = 15000,
        max_full_playback_seconds: float = MAX_FULL_PLAYBACK_SECONDS,
        coverage_min: float | None = None,
        coverage_max: float | None = None,
    ) -> None:
        self.start_timeout_ms = max(2000, int(start_timeout_ms))
        self.stall_timeout_ms = max(1000, int(stall_timeout_ms))
        self.verify_seconds = max(1.0, float(verify_seconds))
        self.mute = mute
        self.verify_full = bool(verify_full)
        self.full_grace_ms = max(0, int(full_grace_ms))
        self.max_full_playback_seconds = max(1.0, float(max_full_playback_seconds))
        if coverage_min is None or coverage_max is None:
            self.coverage_min: float | None = None
            self.coverage_max: float | None = None
        else:
            low = min(max(0.0, float(coverage_min)), 1.0)
            high = min(max(0.0, float(coverage_max)), 1.0)
            self.coverage_min = min(low, high)
            self.coverage_max = max(low, high)
        self._rng = random.Random()

    async def verify(self, page: Any, full: bool | None = None) -> PlaybackOutcome:
        use_full = self.verify_full if full is None else bool(full)
        started = time.monotonic()
        state = await self._safe_evaluate(page, VIDEO_STATE, default={}) or {}
        wait_deadline = started + self.start_timeout_ms / 1000
        saw_error_text = None

        while time.monotonic() < wait_deadline:
            if state.get("error"):
                return self._failed(
                    page, started, state, f"Media error before playback: {state['error']}"
                )
            if state.get("present") and int(state.get("readyState") or 0) >= READY_STATE_HAVE_CURRENT_DATA:
                break
            if not state.get("present") and state.get("errorTexts"):
                saw_error_text = str(state["errorTexts"][0])
                break
            await asyncio.sleep(0.25)
            state = await self._safe_evaluate(page, VIDEO_STATE, default={}) or {}

        if saw_error_text:
            return PlaybackOutcome(
                status=PlaybackStatus.FAILED,
                verified=False,
                start_time_ms=None,
                checked_ms=int((time.monotonic() - started) * 1000),
                message=f"The player showed an error before playback: {saw_error_text[:240]}",
                video_state=state,
            )

        if not state.get("present"):
            return PlaybackOutcome(
                status=PlaybackStatus.NOT_VERIFIED,
                verified=False,
                start_time_ms=None,
                checked_ms=int((time.monotonic() - started) * 1000),
                message="No video element appeared on the page.",
                video_state=state,
            )

        if int(state.get("readyState") or 0) < READY_STATE_HAVE_CURRENT_DATA:
            return PlaybackOutcome(
                status=PlaybackStatus.FAILED,
                verified=False,
                start_time_ms=None,
                checked_ms=int((time.monotonic() - started) * 1000),
                message=(
                    "The video element never became ready "
                    f"(readyState {state.get('readyState')}, "
                    f"frames {state.get('videoWidth')}x{state.get('videoHeight')}) "
                    f"within {self.start_timeout_ms} ms."
                ),
                video_state=state,
                ready_state=state.get("readyState"),
            )

        monitor = await self._safe_evaluate(page, START_PLAYBACK_MONITOR, default={}) or {}
        if not monitor.get("ok"):
            return PlaybackOutcome(
                status=PlaybackStatus.NOT_VERIFIED,
                verified=False,
                start_time_ms=None,
                checked_ms=int((time.monotonic() - started) * 1000),
                message="Playback could not be instrumented (video element disappeared).",
                video_state=state,
            )

        autoplay_blocked = False
        attempt_started = time.monotonic()
        baseline = float(state.get("currentTime") or 0.0)

        if bool(state.get("paused")) or baseline <= 0.05:
            rect = state.get("rect") or {}
            width = float(rect.get("width") or 0)
            height = float(rect.get("height") or 0)
            if width > 20 and height > 20:
                # A real click on the player is the same interaction a viewer
                # makes; it satisfies autoplay policies without touching any
                # engagement control.
                await self._safe_click(
                    page,
                    float(rect.get("x") or 0) + width / 2,
                    float(rect.get("y") or 0) + min(height * 0.4, height / 2),
                )
                await asyncio.sleep(0.35)
            play_result = await self._safe_evaluate(page, REQUEST_PLAY, default={}) or {}
            if play_result.get("blocked"):
                autoplay_blocked = True

        progress_deadline = time.monotonic() + self.start_timeout_ms / 1000
        latest: dict[str, Any] = {}
        start_ms: int | None = None
        max_time = baseline
        last_progress = time.monotonic()
        stalled = False
        playback_error: dict[str, Any] | None = None

        while True:
            latest = await self._safe_evaluate(page, READ_PLAYBACK, default={}) or {}
            playback = (latest.get("playback") or {}) if latest.get("present") else {}
            max_time = max(max_time, float(playback.get("maxTime") or 0.0))
            errors = playback.get("errors") or []
            if errors:
                playback_error = errors[-1]
                break
            if latest.get("error"):
                playback_error = latest["error"]
                break
            if max_time > baseline + MIN_ADVANCE_SECONDS / 3:
                if start_ms is None:
                    start_ms = int((time.monotonic() - attempt_started) * 1000)
                last_progress = max(last_progress, time.monotonic() - 0.01)
                if max_time > baseline + MIN_ADVANCE_SECONDS:
                    break
            if time.monotonic() > progress_deadline:
                break
            await asyncio.sleep(0.2)

        advanced = max_time >= baseline + MIN_ADVANCE_SECONDS

        if not advanced:
            paused = bool(latest.get("paused"))
            watched = time.monotonic() - attempt_started
            if playback_error:
                message = f"Playback failed: {playback_error}"
                status = PlaybackStatus.FAILED
            elif autoplay_blocked or paused:
                status = PlaybackStatus.NOT_VERIFIED
                message = (
                    "Playback could not be started automatically "
                    "(browser autoplay policy); the video itself is reachable."
                )
            elif watched >= self.start_timeout_ms / 1000:
                status = PlaybackStatus.FAILED
                message = (
                    "Playback did not progress: currentTime stayed at "
                    f"{max_time:.2f}s for {int(watched * 1000)} ms."
                )
            else:
                status = PlaybackStatus.NOT_VERIFIED
                message = "Playback started too slowly to be verified."
            outcome = PlaybackOutcome(
                status=status,
                verified=False,
                start_time_ms=start_ms,
                checked_ms=int((time.monotonic() - started) * 1000),
                message=message,
                video_state=latest,
                samples=(latest.get("playback") or {}).get("samples", [])[-20:],
                errors=(latest.get("playback") or {}).get("errors", []),
                stalled=stalled,
                autoplay_blocked=autoplay_blocked,
                advanced_seconds=round(max_time - baseline, 2),
                ready_state=latest.get("readyState"),
            )
            await self._cleanup(page)
            return outcome

        # Keep watching until the Short reaches its end. A fixed sample window
        # cannot prove the rest of the video plays, so in full mode a PASS
        # requires the player to report the end (or the reported duration to be
        # covered) within a bounded window.
        duration = _positive_float(latest.get("duration"))
        duration_known_at_start = duration is not None
        # Randomised coverage: each check watches between the configured minimum
        # and maximum share of the Short, never less than 60 % by default.
        if use_full:
            fraction: float | None = 1.0
        elif self.coverage_min is not None and self.coverage_max is not None:
            fraction = self._rng.uniform(self.coverage_min, self.coverage_max)
        else:
            fraction = None
        target_seconds = duration * fraction if duration is not None and fraction is not None else None
        if fraction is not None:
            if target_seconds is not None:
                remaining = max(2.0, target_seconds - max_time)
                window = min(self.max_full_playback_seconds, remaining + self.full_grace_ms / 1000)
            else:
                # The player usually reports the duration within a second; this
                # shorter window is extended once the duration is known so an
                # unresponsive player cannot hang the check for minutes.
                window = max(self.verify_seconds, 30.0)
        else:
            window = self.verify_seconds
        watch_deadline = time.monotonic() + window
        last_progress = time.monotonic()
        completed = False

        while time.monotonic() < watch_deadline:
            await asyncio.sleep(0.25)
            latest = await self._safe_evaluate(page, READ_PLAYBACK, default={}) or {}
            playback = (latest.get("playback") or {}) if latest.get("present") else {}
            errors = playback.get("errors") or []
            if errors:
                playback_error = errors[-1]
                break
            if latest.get("error"):
                playback_error = latest["error"]
                break

            reported = _positive_float(latest.get("duration"))
            if reported is not None:
                duration = reported
                if fraction is not None:
                    target_seconds = duration * fraction
                    if not duration_known_at_start:
                        duration_known_at_start = True
                        remaining = max(2.0, target_seconds - max_time)
                        watch_deadline = max(
                            watch_deadline,
                            time.monotonic()
                            + min(
                                self.max_full_playback_seconds,
                                remaining + self.full_grace_ms / 1000,
                            ),
                        )
            new_max = max(max_time, float(playback.get("maxTime") or 0.0))
            if new_max > max_time + 0.01:
                max_time = new_max
                last_progress = time.monotonic()

            ended = bool(latest.get("ended"))
            reached_target = ended or (
                target_seconds is not None and max_time >= target_seconds - 0.25
            )
            paused = bool(latest.get("paused"))

            # Buffering (readyState < HAVE_FUTURE_DATA) is not a stall; give it
            # until the bounded deadline before deciding playback failed.
            buffering = int(latest.get("readyState") or 0) < 3
            if fraction is not None:
                if reached_target:
                    completed = True
                    break
                if (
                    not paused
                    and not buffering
                    and (time.monotonic() - last_progress) * 1000 > self.stall_timeout_ms
                ):
                    stalled = True
                    break
                if paused:
                    stalled = True
                    break
            else:
                if ended:
                    completed = True
                    break
                if (
                    not paused
                    and not buffering
                    and (time.monotonic() - last_progress) * 1000 > self.stall_timeout_ms
                ):
                    stalled = True
                    break
                if paused:
                    stalled = True
                    break

        watched_seconds = round(max(0.0, max_time - baseline), 2)
        watched_ms = int(watched_seconds * 1000)
        duration_seconds = round(duration, 2) if duration is not None else None

        await self._cleanup(page)

        if playback_error:
            return PlaybackOutcome(
                status=PlaybackStatus.FAILED,
                verified=False,
                start_time_ms=start_ms,
                checked_ms=watched_ms,
                message=f"Playback failed during verification: {playback_error}",
                video_state=latest,
                samples=(latest.get("playback") or {}).get("samples", [])[-20:],
                errors=(latest.get("playback") or {}).get("errors", []),
                stalled=stalled,
                advanced_seconds=watched_seconds,
                ready_state=latest.get("readyState"),
                completed=False,
                duration_seconds=duration_seconds,
                watched_seconds=watched_seconds,
            )
        if stalled:
            return PlaybackOutcome(
                status=PlaybackStatus.FAILED,
                verified=False,
                start_time_ms=start_ms,
                checked_ms=watched_ms,
                message=(
                    "Playback stalled before the end of the Short: currentTime stopped advancing at "
                    f"{max_time:.2f}s for more than {self.stall_timeout_ms} ms."
                ),
                video_state=latest,
                samples=(latest.get("playback") or {}).get("samples", [])[-20:],
                errors=(latest.get("playback") or {}).get("errors", []),
                stalled=True,
                advanced_seconds=watched_seconds,
                ready_state=latest.get("readyState"),
                completed=False,
                duration_seconds=duration_seconds,
                watched_seconds=watched_seconds,
            )

        if fraction is not None and not completed:
            target_percent = round((fraction or 1.0) * 100)
            if duration_seconds is not None:
                covered = min(100.0, watched_seconds / duration_seconds * 100)
                message = (
                    f"Playback covered {watched_seconds:.1f}s of {duration_seconds:.1f}s "
                    f"({covered:.0f}%) but did not reach the {target_percent}% target "
                    "within the check window."
                )
            else:
                message = (
                    f"Playback covered {watched_seconds:.1f}s, but the player never reported a "
                    "duration or the end of the Short."
                )
            return PlaybackOutcome(
                status=PlaybackStatus.NOT_VERIFIED,
                verified=False,
                start_time_ms=start_ms,
                checked_ms=watched_ms,
                message=message,
                video_state=latest,
                samples=(latest.get("playback") or {}).get("samples", [])[-20:],
                errors=(latest.get("playback") or {}).get("errors", []),
                advanced_seconds=watched_seconds,
                ready_state=latest.get("readyState"),
                completed=False,
                duration_seconds=duration_seconds,
                watched_seconds=watched_seconds,
            )

        if fraction is not None:
            covered = (
                min(100.0, watched_seconds / duration_seconds * 100)
                if duration_seconds
                else 100.0
            )
            message = (
                f"Playback verified: covered {covered:.0f}% of the Short "
                f"({watched_seconds:.1f}s"
                + (f" of {duration_seconds:.1f}s" if duration_seconds else "")
                + f", target {round(fraction * 100)}%)."
            )
        else:
            message = (
                f"Playback verified: currentTime advanced {watched_seconds:.2f}s "
                f"in {int((time.monotonic() - attempt_started) * 1000)} ms."
            )

        return PlaybackOutcome(
            status=PlaybackStatus.VERIFIED,
            verified=True,
            start_time_ms=start_ms,
            checked_ms=watched_ms,
            message=message,
            video_state=latest,
            samples=(latest.get("playback") or {}).get("samples", [])[-20:],
            errors=(latest.get("playback") or {}).get("errors", []),
            advanced_seconds=watched_seconds,
            ready_state=latest.get("readyState"),
            completed=completed,
            duration_seconds=duration_seconds,
            watched_seconds=watched_seconds,
        )

    def _failed(self, page: Any, started: float, state: dict[str, Any], message: str) -> PlaybackOutcome:
        return PlaybackOutcome(
            status=PlaybackStatus.FAILED,
            verified=False,
            start_time_ms=None,
            checked_ms=int((time.monotonic() - started) * 1000),
            message=message,
            video_state=state,
            ready_state=state.get("readyState"),
        )

    @staticmethod
    async def _safe_evaluate(page: Any, script: str, default: Any = None) -> Any:
        try:
            return await page.evaluate(script)
        except Exception:  # noqa: BLE001 - page may have navigated or closed
            return default

    @staticmethod
    async def _safe_click(page: Any, x: float, y: float) -> None:
        try:
            await page.mouse_click(x, y)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _cleanup(page: Any) -> None:
        try:
            await page.evaluate(CLEANUP_PLAYBACK)
        except Exception:  # noqa: BLE001
            pass


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 0 and number != float("inf"):
        return number
    return None
