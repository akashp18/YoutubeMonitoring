from __future__ import annotations

from pathlib import Path

import pytest

from app.domain import Availability, PlaybackStatus, RestrictionType
from app.services.browser_monitor import YouTubeBrowserMonitor
from app.services.playback_verification import PlaybackVerificationService
from app.services.restriction_detection import RestrictionDetectionService
from tests.fakes import FakeBrowser, FakePage
from tests.helpers import (
    accessible_probe,
    captcha_probe,
    constant_playback,
    playback_payload,
    ready_video_state,
    rising_playback,
    unavailable_probe,
)


def build_monitor(tmp_path: Path, page: FakePage, max_retries: int = 0) -> YouTubeBrowserMonitor:
    return YouTubeBrowserMonitor(
        browser_manager=FakeBrowser([page]),
        detection=RestrictionDetectionService(),
        playback=PlaybackVerificationService(
            start_timeout_ms=3000,
            stall_timeout_ms=800,
            verify_seconds=1.0,
            verify_full=False,
        ),
        page_timeout_ms=5000,
        max_retries=max_retries,
        settle_timeout_ms=2000,
        screenshot_on_failure=True,
        evidence_dir=tmp_path,
    )


async def test_pass_when_video_plays(tmp_path: Path) -> None:
    page = FakePage(
        probe=accessible_probe(),
        video_states=[ready_video_state()],
        playback=rising_playback(),
    )
    outcome = await build_monitor(tmp_path, page).check_video(
        "abcdefghijk", run_id=7, playback_seconds=1.0
    )
    assert outcome.availability == Availability.PASS
    assert outcome.playback_status == PlaybackStatus.VERIFIED
    assert outcome.playback_verified is True
    assert outcome.restriction_type == RestrictionType.NONE
    assert outcome.http_status == 200
    assert page.goto_calls and "/shorts/abcdefghijk" in page.goto_calls[0]
    assert page.goto_calls[0].count("abcdefghijk") == 1
    assert outcome.evidence["browser_version"] == "test-browser-1.0"
    assert page.closed is True


async def test_restricted_video_captures_screenshot(tmp_path: Path) -> None:
    page = FakePage(probe=unavailable_probe())
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk", run_id=9)
    assert outcome.availability == Availability.RESTRICTED
    assert outcome.restriction_type == RestrictionType.UNAVAILABLE
    assert outcome.playback_status == PlaybackStatus.SKIPPED
    assert outcome.screenshot_path is not None
    assert Path(outcome.screenshot_path).exists()
    assert outcome.evidence["page_signals"]["verdict"]["verdict"] == "RESTRICTED"


async def test_captcha_is_inconclusive(tmp_path: Path) -> None:
    page = FakePage(probe=captcha_probe())
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk")
    assert outcome.availability == Availability.INCONCLUSIVE
    assert outcome.restriction_type == RestrictionType.CAPTCHA


async def test_http_404_means_unavailable(tmp_path: Path) -> None:
    page = FakePage(goto_status=404)
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk")
    assert outcome.availability == Availability.RESTRICTED
    assert outcome.restriction_type == RestrictionType.UNAVAILABLE
    assert outcome.http_status == 404


async def test_network_failure_is_failed_and_retried(tmp_path: Path, monkeypatch) -> None:
    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.browser_monitor.asyncio.sleep", fast_sleep)
    page = FakePage(goto_error=Exception("Page.goto: net::ERR_CONNECTION_REFUSED at https://x"))
    monitor = YouTubeBrowserMonitor(
        browser_manager=FakeBrowser([page, page]),
        detection=RestrictionDetectionService(),
        playback=PlaybackVerificationService(
            start_timeout_ms=3000,
            stall_timeout_ms=800,
            verify_seconds=1.0,
            verify_full=False,
        ),
        page_timeout_ms=5000,
        max_retries=1,
        settle_timeout_ms=2000,
        screenshot_on_failure=True,
        evidence_dir=tmp_path,
    )
    outcome = await monitor.check_video("abcdefghijk")
    assert outcome.availability == Availability.FAILED
    assert outcome.network_error is True
    assert outcome.attempts == 2
    assert "DNS" in outcome.diagnostic_message or "connection" in outcome.diagnostic_message.lower()


async def test_navigation_timeout_is_explained(tmp_path: Path) -> None:
    page = FakePage(goto_error=Exception("Page.goto: Timeout 5000ms exceeded."))
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk")
    assert outcome.availability == Availability.FAILED
    assert "navigation timeout" in outcome.diagnostic_message


async def test_stalled_playback_is_failed(tmp_path: Path) -> None:
    values = iter([0.6, 1.2] + [1.2] * 100)

    def playback():
        return playback_payload(next(values))

    page = FakePage(probe=accessible_probe(), video_states=[ready_video_state()], playback=playback)
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk", playback_seconds=2.0)
    assert outcome.availability == Availability.FAILED
    assert outcome.restriction_type == RestrictionType.PLAYBACK_ERROR
    assert outcome.playback_status == PlaybackStatus.FAILED


async def test_autoplay_block_is_a_warning(tmp_path: Path) -> None:
    page = FakePage(
        probe=accessible_probe(),
        video_states=[ready_video_state(paused=True)],
        playback=constant_playback(0.0, paused=True),
        play_blocked=True,
    )
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk", playback_seconds=1.0)
    assert outcome.availability == Availability.WARNING
    assert outcome.playback_status == PlaybackStatus.NOT_VERIFIED


async def test_playing_video_with_unrelated_unavailable_text_passes(tmp_path: Path) -> None:
    probe = accessible_probe()
    probe["bodyText"] = "This video is unavailable (a stale element elsewhere)."
    page = FakePage(probe=probe, video_states=[ready_video_state()], playback=rising_playback())
    outcome = await build_monitor(tmp_path, page).check_video("abcdefghijk", playback_seconds=1.0)
    assert outcome.availability == Availability.PASS


async def test_video_is_not_reloaded_within_a_check(tmp_path: Path) -> None:
    page = FakePage(
        probe=accessible_probe(),
        video_states=[ready_video_state()],
        playback=rising_playback(),
    )
    await build_monitor(tmp_path, page).check_video("abcdefghijk", playback_seconds=1.0)
    assert len(page.goto_calls) == 1
