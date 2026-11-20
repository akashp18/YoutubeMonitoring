from __future__ import annotations

from app.domain import PlaybackStatus
from app.services.page_scripts import PROBE_PAGE, READ_PLAYBACK
from app.services.playback_verification import PlaybackVerificationService
from tests.fakes import FakePage
from tests.helpers import (
    constant_playback,
    playback_payload,
    ready_video_state,
    rising_playback,
)


def service(verify_seconds: float = 1.0, start_timeout: int = 3000, stall: int = 1000):
    return PlaybackVerificationService(
        start_timeout_ms=start_timeout,
        stall_timeout_ms=stall,
        verify_seconds=verify_seconds,
        verify_full=False,
    )


async def test_verified_playback_advances_current_time() -> None:
    page = FakePage(
        probe={"video": {"present": True}},
        video_states=[ready_video_state()],
        playback=rising_playback(),
    )
    outcome = await service().verify(page)
    assert outcome.status == PlaybackStatus.VERIFIED
    assert outcome.verified is True
    assert outcome.start_time_ms is not None
    assert outcome.advanced_seconds > 0
    assert page.clicks, "a real click should be used to start playback"
    assert page.play_requests >= 1


async def test_full_playback_plays_to_the_end_of_the_short() -> None:
    clock = {"time": 0.0}

    def playback():
        clock["time"] = min(clock["time"] + 3.0, 12.0)
        return playback_payload(clock["time"], duration=12.0, ended=False)

    page = FakePage(
        video_states=[ready_video_state(duration=12.0)],
        playback=playback,
    )
    outcome = await PlaybackVerificationService(
        start_timeout_ms=3000,
        stall_timeout_ms=1000,
        verify_full=True,
        full_grace_ms=500,
        max_full_playback_seconds=10.0,
    ).verify(page)
    assert outcome.status == PlaybackStatus.VERIFIED
    assert outcome.completed is True
    assert outcome.duration_seconds == 12.0
    assert outcome.watched_seconds >= 11.5
    assert "covered 100%" in outcome.message
    assert "target 100%" in outcome.message


async def test_random_coverage_watches_between_the_configured_bounds() -> None:
    clock = {"time": 0.0}

    def playback():
        clock["time"] = min(clock["time"] + 3.0, 20.0)
        return playback_payload(clock["time"], duration=20.0)

    page = FakePage(
        video_states=[ready_video_state(duration=20.0)],
        playback=playback,
    )
    outcome = await PlaybackVerificationService(
        start_timeout_ms=3000,
        stall_timeout_ms=1000,
        verify_full=False,
        coverage_min=0.5,
        coverage_max=0.5,
        full_grace_ms=500,
        max_full_playback_seconds=10.0,
    ).verify(page)
    assert outcome.status == PlaybackStatus.VERIFIED
    assert outcome.completed is True
    assert outcome.duration_seconds == 20.0
    assert 9.5 <= outcome.watched_seconds <= 13.0
    assert 45 <= (outcome.watched_seconds / 20.0) * 100 <= 65


async def test_coverage_short_of_the_minimum_is_not_verified() -> None:
    page = FakePage(
        video_states=[ready_video_state(duration=20.0)],
        playback=constant_playback(3.0, duration=20.0),
    )
    outcome = await PlaybackVerificationService(
        start_timeout_ms=3000,
        stall_timeout_ms=5000,
        verify_full=False,
        coverage_min=0.6,
        coverage_max=1.0,
        full_grace_ms=0,
        max_full_playback_seconds=1.0,
    ).verify(page)
    assert outcome.status == PlaybackStatus.NOT_VERIFIED
    assert outcome.completed is False
    assert "target" in outcome.message


async def test_full_playback_that_never_reaches_the_end_is_not_verified() -> None:
    page = FakePage(
        video_states=[ready_video_state(duration=60.0)],
        playback=constant_playback(2.4, duration=60.0),
    )
    outcome = await PlaybackVerificationService(
        start_timeout_ms=3000,
        stall_timeout_ms=5000,
        verify_full=True,
        full_grace_ms=0,
        max_full_playback_seconds=1.0,
    ).verify(page)
    assert outcome.status == PlaybackStatus.NOT_VERIFIED
    assert outcome.verified is False
    assert outcome.completed is False
    assert "did not reach the 100% target" in outcome.message


async def test_full_playback_stall_before_the_end_fails() -> None:
    values = iter([2.0, 3.0] + [3.0] * 100)

    def playback():
        return playback_payload(next(values), duration=60.0)

    page = FakePage(
        video_states=[ready_video_state(duration=60.0)],
        playback=playback,
    )
    outcome = await PlaybackVerificationService(
        start_timeout_ms=3000,
        stall_timeout_ms=300,
        verify_full=True,
        full_grace_ms=0,
        max_full_playback_seconds=5.0,
    ).verify(page)
    assert outcome.status == PlaybackStatus.FAILED
    assert outcome.stalled is True
    assert outcome.completed is False
    assert "before the end" in outcome.message


async def test_playback_stall_is_failed() -> None:
    values = iter([0.6, 1.2] + [1.2] * 100)

    def playback():
        return playback_payload(next(values))

    page = FakePage(
        video_states=[ready_video_state()],
        playback=playback,
    )
    outcome = await service(verify_seconds=2.0, stall=700).verify(page)
    assert outcome.status == PlaybackStatus.FAILED
    assert outcome.stalled is True
    assert "stalled" in outcome.message.lower()


async def test_media_error_is_failed() -> None:
    page = FakePage(
        video_states=[ready_video_state()],
        playback=lambda: playback_payload(
            0.0, errors=[{"code": 2, "message": "MEDIA_ERR_NETWORK"}]
        ),
    )
    outcome = await service().verify(page)
    assert outcome.status == PlaybackStatus.FAILED
    assert "MEDIA_ERR_NETWORK" in outcome.message


async def test_video_that_never_becomes_ready_is_failed() -> None:
    page = FakePage(
        video_states=[ready_video_state(readyState=1, videoWidth=0, videoHeight=0)],
        playback=constant_playback(0.0),
    )
    outcome = await service(start_timeout=2000).verify(page)
    assert outcome.status == PlaybackStatus.FAILED
    assert "never became ready" in outcome.message


async def test_autoplay_block_is_not_verified_not_failed() -> None:
    page = FakePage(
        video_states=[ready_video_state(paused=True)],
        playback=constant_playback(0.0, paused=True),
        play_blocked=True,
    )
    outcome = await service(start_timeout=1500).verify(page)
    assert outcome.status == PlaybackStatus.NOT_VERIFIED
    assert outcome.autoplay_blocked is True
    assert outcome.verified is False


async def test_missing_video_element_is_not_verified() -> None:
    page = FakePage(video_states=[{"present": False, "errorTexts": []}])
    outcome = await service(start_timeout=1500).verify(page)
    assert outcome.status == PlaybackStatus.NOT_VERIFIED
    assert "No video element" in outcome.message


async def test_scripts_target_expected_dom_structures() -> None:
    # Guards against accidental renaming of the injected scripts.
    assert "ytd-reel-video-renderer[is-active]" in PROBE_PAGE
    assert "window.__ytplay" in READ_PLAYBACK
