"""ShortsDiscoveryService.

Discovery strategy, in order of reliability:

1. YouTube Data API (when a key is configured): uploads playlist + duration
   filter + a Shorts URL probe to drop regular uploads.
2. Playwright channel Shorts tab: scrolls until the grid stops growing and
   harvests `ytInitialData`, innertube payloads and DOM anchors.
3. Plain HTML: first page only, flagged as truncated.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.domain import DiscoverySource
from app.services.channel_resolver import DESKTOP_UA, HtmlFetcher, HttpxFetcher
from app.services.youtube_api import YouTubeAPIError, YouTubeDataAPI
from app.services.youtube_parse import parse_shorts_from_html

logger = logging.getLogger(__name__)

MAX_SHORT_DURATION_SECONDS = 180
MAX_DISCOVERY_VIDEOS = 1000


@dataclass
class DiscoveryResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    source: str = DiscoverySource.BROWSER
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "source": self.source,
            "truncated": self.truncated,
            "notes": self.notes,
        }


class ShortsUrlProbe:
    """Cheap check whether /shorts/<id> is a Shorts page (not a redirect)."""

    def __init__(self, client: Any | None = None, timeout: float = 15.0) -> None:
        self._client = client
        self.timeout = timeout

    async def is_short(self, video_id: str) -> bool | None:
        import httpx

        url = f"https://www.youtube.com/shorts/{video_id}"
        headers = {"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"}
        try:
            if self._client is not None:
                response = await self._client.get(
                    url, headers=headers, follow_redirects=False
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                    response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return None
        if response.status_code in {301, 302, 303, 307, 308}:
            location = (response.headers or {}).get("location", "")
            return "/shorts/" in location
        if response.status_code == 200:
            return True
        return None


class ShortsDiscoveryService:
    def __init__(
        self,
        browser_manager: Any | None = None,
        api: YouTubeDataAPI | None = None,
        fetcher: HtmlFetcher | None = None,
        max_scrolls: int = 12,
        scroll_delay_ms: int = 900,
        page_timeout_ms: int = 25000,
        probe: ShortsUrlProbe | None = None,
        max_videos: int = MAX_DISCOVERY_VIDEOS,
    ) -> None:
        self.browser = browser_manager
        self.api = api
        self.fetcher = fetcher or HttpxFetcher()
        self.max_scrolls = max(0, int(max_scrolls))
        self.scroll_delay_ms = max(200, int(scroll_delay_ms))
        self.page_timeout_ms = page_timeout_ms
        self.probe = probe
        self.max_videos = max_videos

    async def discover(self, channel: dict[str, Any]) -> DiscoveryResult:
        notes: list[str] = []
        if self.api is not None and channel.get("channel_id"):
            try:
                return await self._discover_via_api(channel)
            except YouTubeAPIError as exc:
                notes.append(f"YouTube Data API unavailable ({exc.code}): {exc}")
                logger.warning("Data API discovery failed for %s: %s", channel.get("channel_id"), exc)
            except Exception as exc:  # noqa: BLE001 - fall back to the browser
                notes.append(f"YouTube Data API discovery failed: {exc}")
                logger.exception("Unexpected Data API discovery failure")

        shorts_url = channel.get("shorts_url") or (
            f"https://www.youtube.com/channel/{channel.get('channel_id')}/shorts"
            if channel.get("channel_id")
            else None
        )
        if not shorts_url:
            raise DiscoveryError("The channel has no resolved Shorts URL.", "unresolved_channel")

        if self.browser is not None:
            try:
                result = await self._discover_via_browser(shorts_url)
                result.notes = notes + result.notes
                return result
            except DiscoveryError:
                raise
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Browser discovery failed: {exc}")
                logger.warning("Browser discovery failed: %s", exc)

        result = await self._discover_via_html(shorts_url)
        result.notes = notes + result.notes
        return result

    async def _discover_via_api(self, channel: dict[str, Any]) -> DiscoveryResult:
        assert self.api is not None
        channel_id = channel.get("channel_id")
        if not channel_id:
            raise YouTubeAPIError("Channel ID not resolved", "unresolved")
        details = await self.api.channel(channel_id=channel_id)
        if not details:
            raise YouTubeAPIError("Channel not found by the Data API", "not_found")
        uploads = (
            (details.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
        )
        if not uploads:
            raise YouTubeAPIError("No uploads playlist for channel", "not_found")
        playlist = await self.api.playlist_videos(uploads, max_videos=self.max_videos)
        if not playlist:
            return DiscoveryResult(items=[], source=DiscoverySource.API)

        ids = [item["video_id"] for item in playlist]
        details_list = await self.api.videos(ids[: self.max_videos])
        candidates = [
            item
            for item in details_list
            if item.get("duration_seconds") is None
            or int(item["duration_seconds"]) <= MAX_SHORT_DURATION_SECONDS
        ]
        confirmed = await self._confirm_shorts(candidates)
        notes: list[str] = []
        if self.probe is None:
            notes.append(
                "Shorts were identified by upload duration; configure Playwright for "
                "exact Shorts-tab verification."
            )
        return DiscoveryResult(
            items=confirmed,
            source=DiscoverySource.API,
            truncated=len(playlist) >= self.max_videos,
            notes=notes,
        )

    async def _confirm_shorts(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.probe is None:
            return candidates
        semaphore = asyncio.Semaphore(4)

        async def check(item: dict[str, Any]) -> dict[str, Any] | None:
            async with semaphore:
                verdict = await self.probe.is_short(item["video_id"])
            if verdict is False:
                return None
            return item

        results = await asyncio.gather(*(check(item) for item in candidates))
        confirmed = [item for item in results if item]
        # Never return an empty list because the probe itself failed.
        return confirmed if confirmed or not candidates else candidates

    async def _discover_via_browser(self, shorts_url: str) -> DiscoveryResult:
        assert self.browser is not None
        session = await self.browser.open_discovery_session()
        collected: dict[str, dict[str, Any]] = {}
        truncated = False
        stagnant = 0
        try:
            await session.open(shorts_url, self.page_timeout_ms)
            for scroll_index in range(self.max_scrolls + 1):
                snapshot = await session.snapshot()
                before = len(collected)
                for item in snapshot.get("items") or []:
                    video_id = item.get("video_id")
                    if not video_id:
                        continue
                    existing = collected.get(video_id)
                    if existing:
                        existing["title"] = existing.get("title") or item.get("title")
                        existing["thumbnail_url"] = (
                            existing.get("thumbnail_url") or item.get("thumbnail_url")
                        )
                    else:
                        collected[video_id] = {
                            "video_id": video_id,
                            "title": item.get("title"),
                            "thumbnail_url": item.get("thumbnail_url"),
                            "video_url": f"https://www.youtube.com/shorts/{video_id}",
                            "duration_seconds": item.get("duration_seconds"),
                        }
                grew = len(collected) > before
                scroll_height = int(snapshot.get("scrollHeight") or 0)
                scroll_y = int(snapshot.get("scrollY") or 0)
                viewport = int(snapshot.get("viewport") or 800)
                remaining = scroll_height - (scroll_y + viewport)
                if grew:
                    stagnant = 0
                elif remaining <= 150:
                    stagnant += 1
                if stagnant >= 2:
                    break
                if scroll_index >= self.max_scrolls:
                    truncated = remaining > 150
                    break
                await session.scroll(int(viewport * 0.85) or 700)
                await session.wait(self.scroll_delay_ms)
            if not collected:
                raise DiscoveryError(
                    "No Shorts were found on the channel Shorts tab. The channel may have no "
                    "public Shorts, or YouTube did not serve the Shorts grid to this browser.",
                    "empty",
                )
        finally:
            await session.close()

        items = list(collected.values())[: self.max_videos]
        return DiscoveryResult(items=items, source=DiscoverySource.BROWSER, truncated=truncated)

    async def _discover_via_html(self, shorts_url: str) -> DiscoveryResult:
        status, html = await self.fetcher.fetch(shorts_url)
        if status >= 400:
            raise DiscoveryError(
                f"YouTube returned HTTP {status} for the channel Shorts tab.", "http_error"
            )
        items = parse_shorts_from_html(html)
        if not items:
            raise DiscoveryError(
                "No Shorts could be read from the channel HTML. YouTube may be serving a "
                "consent page or an interface this build does not understand.",
                "no_items",
            )
        return DiscoveryResult(
            items=items[: self.max_videos],
            source=DiscoverySource.HTML,
            truncated=True,
            notes=["HTML fallback only reads the first page of the Shorts tab."],
        )


class DiscoveryError(Exception):
    def __init__(self, message: str, code: str = "discovery_failed") -> None:
        super().__init__(message)
        self.code = code
