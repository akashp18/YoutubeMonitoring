from __future__ import annotations

import copy
from typing import Any, Callable, Iterable

from app.domain import Availability, PlaybackStatus, RestrictionType
from app.services.browser_monitor import MonitorOutcome
from app.services.channel_resolver import ResolvedChannel


# --------------------------------------------------------------------------- #
# Browser doubles
# --------------------------------------------------------------------------- #


class FakePage:
    """Implements the adapter surface used by the monitor.

    `probe` is what PROBE_PAGE returns; `video_states` feeds VIDEO_STATE;
    `playback` is either a callable returning a READ_PLAYBACK payload or a list
    of payloads (the last one is reused).
    """

    def __init__(
        self,
        probe: dict[str, Any] | None = None,
        video_states: list[dict[str, Any]] | None = None,
        playback: Callable[[], dict[str, Any]] | list[dict[str, Any]] | None = None,
        goto_status: int = 200,
        goto_error: Exception | None = None,
        play_blocked: bool = False,
    ) -> None:
        self.probe = probe or {}
        self.video_states = video_states or []
        self._playback = playback
        self.goto_status = goto_status
        self.goto_error = goto_error
        self.play_blocked = play_blocked
        self.goto_calls: list[str] = []
        self.play_requests = 0
        self.clicks: list[tuple[float, float]] = []
        self.screenshots: list[str] = []
        self.closed = False

    async def goto(self, url: str, timeout_ms: int) -> int | None:
        self.goto_calls.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        return self.goto_status

    async def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        if "errorSelectors" in expression:
            return copy.deepcopy(self.probe)
        if "const state = window.__ytplay" in expression:
            return self._read_playback()
        if "window.__ytplay = state" in expression:
            return {"ok": True, "muted": True, "paused": True, "currentTime": 0, "readyState": 4}
        if "delete window.__ytplay" in expression:
            return True
        if "video.play()" in expression:
            self.play_requests += 1
            return {"ok": True, "paused": False, "currentTime": 0, "blocked": self.play_blocked}
        if "errorTexts: errors" in expression:
            return self._video_state()
        return None

    def _video_state(self) -> dict[str, Any]:
        if not self.video_states:
            return {"present": False, "errorTexts": []}
        if len(self.video_states) > 1:
            return copy.deepcopy(self.video_states.pop(0))
        return copy.deepcopy(self.video_states[0])

    def _read_playback(self) -> dict[str, Any]:
        payload = self._playback
        if callable(payload):
            return payload()
        if isinstance(payload, list) and payload:
            if len(payload) > 1:
                return payload.pop(0)
            return payload[0]
        return {
            "present": False,
            "playback": {"maxTime": 0.0, "samples": [], "errors": []},
        }

    async def mouse_click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))

    async def screenshot(self, path: str) -> None:
        self.screenshots.append(path)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nfake-image")

    async def close(self) -> None:
        self.closed = True

    def extra_evidence(self) -> dict[str, Any]:
        return {
            "console_errors": ["console error from page"],
            "page_errors": [],
            "failed_requests": [],
            "browser_version": "test-browser-1.0",
        }


class FakeBrowser:
    def __init__(self, pages: Iterable[FakePage] | None = None, session: Any | None = None):
        self._pages = list(pages or [])
        self._session = session
        self.created: list[FakePage] = []
        self.browser_version = "test-browser-1.0"
        self.closed = False

    async def new_page(self) -> FakePage:
        if self._pages:
            page = self._pages.pop(0)
        else:
            page = FakePage()
        self.created.append(page)
        return page

    async def open_discovery_session(self) -> Any:
        return self._session

    async def ensure_started(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeDiscoverySession:
    def __init__(self, snapshots: list[dict[str, Any]]):
        self.snapshots = list(snapshots)
        self.opened_url: str | None = None
        self.scrolled = 0
        self.closed = False

    async def open(self, url: str, timeout_ms: int) -> None:
        self.opened_url = url

    async def snapshot(self) -> dict[str, Any]:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    async def scroll(self, dy: int) -> None:
        self.scrolled += dy

    async def wait(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Resolution / discovery doubles
# --------------------------------------------------------------------------- #


class FakeResolver:
    def __init__(self, resolved: ResolvedChannel | None = None, error: Exception | None = None):
        self.resolved = resolved or ResolvedChannel(
            channel_id="UCabcdefghijklmnopqrstuv",
            channel_name="Example Channel",
            handle="examplechannel",
            canonical_url="https://www.youtube.com/@examplechannel",
            shorts_url="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/shorts",
        )
        self.error = error
        self.calls: list[Any] = []

    async def resolve(self, parsed: Any) -> ResolvedChannel:
        self.calls.append(parsed)
        if self.error:
            raise self.error
        return self.resolved

    async def resolve_id(self, channel_id: str) -> ResolvedChannel:
        self.calls.append(channel_id)
        if self.error:
            raise self.error
        return self.resolved


class FakeDiscoveryService:
    def __init__(self, results: list[Any]):
        self.results = list(results)
        self.calls = 0
        self.max_scrolls = 0
        self.scroll_delay_ms = 0
        self.page_timeout_ms = 0

    async def discover(self, channel: dict[str, Any]) -> Any:
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class QueueMonitor:
    def __init__(self, outcomes: dict[str, list[MonitorOutcome]] | list[MonitorOutcome]):
        self._by_video = outcomes if isinstance(outcomes, dict) else None
        self._queue = [] if isinstance(outcomes, dict) else list(outcomes)
        self.calls: list[str] = []

    async def check_video(
        self,
        video_id: str,
        run_id: int | None = None,
        capture_screenshot: bool = True,
        playback_seconds: float | None = None,
    ) -> MonitorOutcome:
        self.calls.append(video_id)
        if self._by_video is not None:
            queue = self._by_video.get(video_id) or []
            if queue:
                return queue.pop(0)
            return pass_outcome()
        if self._queue:
            return self._queue.pop(0)
        return pass_outcome()


def pass_outcome(start_ms: int = 400) -> MonitorOutcome:
    return MonitorOutcome(
        availability=Availability.PASS,
        playback_status=PlaybackStatus.VERIFIED,
        restriction_type=RestrictionType.NONE,
        diagnostic_message="Playback verified: currentTime advanced 3.0s in 900 ms.",
        playback_verified=True,
        playback_start_ms=start_ms,
        playback_checked_ms=5000,
        page_load_ms=900,
        ready_state=4,
        video_width=720,
        video_height=1280,
        evidence={"console_errors": [], "page_errors": [], "failed_requests": []},
    )


def restricted_outcome(
    restriction: str = RestrictionType.UNAVAILABLE,
    status: str = Availability.RESTRICTED,
    message: str = "YouTube reports that the video is unavailable.",
) -> MonitorOutcome:
    return MonitorOutcome(
        availability=status,
        playback_status=PlaybackStatus.SKIPPED,
        restriction_type=restriction,
        diagnostic_message=message,
        page_load_ms=700,
        evidence={
            "console_errors": [],
            "page_errors": [],
            "failed_requests": [],
            "page_signals": {"verdict": {"verdict": "RESTRICTED"}},
        },
    )


# --------------------------------------------------------------------------- #
# HTTP doubles
# --------------------------------------------------------------------------- #


class FakeHttpResponse:
    def __init__(self, status_code: int = 200, payload: Any | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self) -> Any:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeHttpResponse] | None = None, status: int = 200):
        self.responses = list(responses or [])
        self.status = status
        self.posts: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.requests.append((url, kwargs))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        if self.responses:
            return self.responses[0]
        return FakeHttpResponse(status_code=self.status, payload={}, text="")

    async def post(self, url: str, json: Any | None = None, timeout: Any = None) -> FakeHttpResponse:
        self.posts.append(json or {})
        return FakeHttpResponse(status_code=self.status, payload={})


class FakeHtmlFetcher:
    def __init__(self, responses: dict[str, tuple[int, str]] | list[tuple[int, str]]):
        self.responses = responses
        self.calls: list[str] = []

    async def fetch(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        if isinstance(self.responses, dict):
            return self.responses.get(url, (404, "not found"))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeYouTubeAPI:
    def __init__(
        self,
        playlist: list[dict[str, Any]],
        details: list[dict[str, Any]] | None = None,
    ):
        self.playlist = playlist
        self.details = details if details is not None else playlist
        self.video_calls: list[list[str]] = []

    async def channel(self, channel_id: str | None = None, handle: str | None = None) -> dict[str, Any]:
        return {
            "id": channel_id or "UCabcdefghijklmnopqrstuv",
            "snippet": {"title": "Example Channel"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUabcdefghijklmnopqrstuv"}},
        }

    async def playlist_videos(self, playlist_id: str, max_videos: int = 1000) -> list[dict[str, Any]]:
        return self.playlist[:max_videos]

    async def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        self.video_calls.append(list(video_ids))
        wanted = set(video_ids)
        return [item for item in self.details if item["video_id"] in wanted]


class FakeProbe:
    def __init__(self, verdicts: dict[str, bool | None] | None = None):
        self.verdicts = verdicts or {}
        self.calls: list[str] = []

    async def is_short(self, video_id: str) -> bool | None:
        self.calls.append(video_id)
        return self.verdicts.get(video_id, True)
