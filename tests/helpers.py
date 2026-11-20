from __future__ import annotations

from typing import Any


def base_signals(**overrides: Any) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "url": "https://www.youtube.com/shorts/abcdefghijk?hl=en",
        "title": "Example Short - YouTube",
        "readyState": "complete",
        "playability": {
            "status": "OK",
            "reason": "",
            "subreason": "",
            "hasVideoDetails": True,
            "isLive": False,
        },
        "errorTexts": [],
        "videoCount": 1,
        "video": {
            "present": True,
            "readyState": 4,
            "networkState": 1,
            "paused": False,
            "ended": False,
            "muted": True,
            "currentTime": 0.0,
            "duration": 30.0,
            "videoWidth": 720,
            "videoHeight": 1280,
            "playbackRate": 1,
            "seekable": True,
            "error": None,
            "rect": {"x": 0, "y": 0, "width": 720, "height": 1280},
            "quality": {"dropped": 0, "total": 30},
        },
        "bodyText": "Example page with a playing Short.",
        "hasChallenge": False,
        "consentWall": False,
        "loginWall": False,
        "mon": {"consoleErrors": [], "pageErrors": [], "rejected": []},
    }
    signals.update(overrides)
    return signals


def accessible_probe(**overrides: Any) -> dict[str, Any]:
    return base_signals(**overrides)


def unavailable_probe(text: str = "This video is unavailable", **overrides: Any) -> dict[str, Any]:
    signals = base_signals(
        playability={
            "status": "UNPLAYABLE",
            "reason": text,
            "subreason": "",
            "hasVideoDetails": False,
            "isLive": False,
        },
        errorTexts=[text],
        videoCount=0,
        video={"present": False},
        bodyText=text,
    )
    signals.update(overrides)
    return signals


def age_gated_probe() -> dict[str, Any]:
    return base_signals(
        playability={
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm your age",
            "subreason": "This video may be inappropriate for some users.",
            "hasVideoDetails": True,
            "isLive": False,
        },
        video={"present": False},
        bodyText="Sign in to confirm your age",
    )


def captcha_probe() -> dict[str, Any]:
    return base_signals(
        playability={"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"},
        hasChallenge=True,
        video={"present": False},
        bodyText="Confirm you're not a bot",
    )


def no_player_probe() -> dict[str, Any]:
    return base_signals(
        playability=None,
        errorTexts=[],
        videoCount=0,
        video={"present": False},
        bodyText="Some unrelated page text with no player.",
    )


def ready_video_state(**overrides: Any) -> dict[str, Any]:
    state = {
        "present": True,
        "readyState": 4,
        "paused": False,
        "muted": True,
        "ended": False,
        "currentTime": 0.0,
        "duration": 30.0,
        "videoWidth": 720,
        "videoHeight": 1280,
        "error": None,
        "rect": {"x": 0, "y": 0, "width": 720, "height": 1280},
        "errorTexts": [],
    }
    state.update(overrides)
    return state


def playback_payload(
    max_time: float,
    paused: bool = False,
    ended: bool = False,
    errors: list[dict[str, Any]] | None = None,
    duration: float = 30.0,
) -> dict[str, Any]:
    return {
        "present": True,
        "paused": paused,
        "ended": ended,
        "muted": True,
        "currentTime": max_time,
        "duration": duration,
        "readyState": 4,
        "networkState": 1,
        "videoWidth": 720,
        "videoHeight": 1280,
        "error": None,
        "quality": {"dropped": 0, "total": 30},
        "playback": {
            "maxTime": max_time,
            "samples": [{"t": 0, "ct": max_time, "paused": paused, "rs": 4}],
            "errors": errors or [],
            "blocked": False,
        },
    }


def rising_playback(start: float = 0.0, step: float = 0.6):
    clock = {"time": start}

    def read() -> dict[str, Any]:
        clock["time"] += step
        return playback_payload(clock["time"])

    return read


def constant_playback(value: float = 0.0, paused: bool = False, duration: float = 30.0):
    def read() -> dict[str, Any]:
        return playback_payload(value, paused=paused, duration=duration)

    return read
