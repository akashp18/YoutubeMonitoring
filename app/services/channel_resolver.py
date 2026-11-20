"""ChannelResolver: turn a user supplied channel URL into a stable channel id."""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.services.channel_url import CHANNEL_ID_RE, ParsedChannelUrl, looks_like_channel_id

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_CANONICAL_RE = re.compile(
    r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
    re.IGNORECASE,
)
_CANONICAL_RE_REVERSED = re.compile(
    r'<link[^>]+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"[^>]+rel="canonical"',
    re.IGNORECASE,
)
_CHANNEL_ID_JSON_RE = re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"')
_EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_HANDLE_RE = re.compile(r'"vanityChannelUrl":"https://www\.youtube\.com/(@[A-Za-z0-9._-]+)"')

_CONSENT_MARKERS = (
    "before you continue to youtube",
    "we use cookies and data",
    "consent.youtube.com",
)
_CAPTCHA_MARKERS = (
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "unusual traffic",
    "verify you're human",
    "verify you’re human",
    "automated queries",
)
_NOT_FOUND_MARKERS = (
    "this channel does not exist",
    "this page isn't available",
    "this page isn’t available",
    "404 not found",
)


class ChannelResolutionError(Exception):
    def __init__(self, message: str, code: str = "resolution_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ResolvedChannel:
    channel_id: str
    channel_name: str | None
    handle: str | None
    canonical_url: str
    shorts_url: str
    source: str = "html"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "handle": self.handle,
            "canonical_url": self.canonical_url,
            "shorts_url": self.shorts_url,
            "source": self.source,
            "warnings": list(self.warnings),
        }


class HtmlFetcher(Protocol):
    async def fetch(self, url: str) -> tuple[int, str]: ...


class HttpxFetcher:
    """Plain HTTP fetcher with browser-like headers."""

    def __init__(self, timeout: float = 25.0, client: httpx.AsyncClient | None = None) -> None:
        self.timeout = timeout
        self._client = client

    async def fetch(self, url: str) -> tuple[int, str]:
        headers = {
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers, follow_redirects=True)
            else:
                async with httpx.AsyncClient(
                    timeout=self.timeout, follow_redirects=True, headers=headers
                ) as client:
                    response = await client.get(url)
            return response.status_code, response.text
        except httpx.HTTPError as exc:
            raise ChannelResolutionError(
                f"Could not reach YouTube: {exc.__class__.__name__}", "network_error"
            ) from exc


class BrowserFetcher:
    """Fallback fetcher that renders the page in Playwright.

    Used only when plain HTTP is blocked or returns no channel metadata. It
    never solves challenges: if YouTube asks for a CAPTCHA we report that.
    """

    def __init__(self, browser_manager: Any) -> None:
        self._browser_manager = browser_manager

    async def fetch(self, url: str) -> tuple[int, str]:
        import asyncio

        page = await self._browser_manager.new_page()
        try:
            status = await page.goto(url, 30000)
            await asyncio.sleep(1.2)
            content = await page.evaluate("() => document.documentElement.outerHTML")
            return (status or 200), str(content or "")
        except Exception as exc:  # noqa: BLE001 - surfaced as a resolution failure
            raise ChannelResolutionError(
                f"Browser resolution failed: {exc.__class__.__name__}: {exc}", "browser_error"
            ) from exc
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


def _text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html_module.unescape(value).strip()
    return cleaned or None


class ChannelResolver:
    def __init__(self, fetcher: HtmlFetcher | None = None, fallback_fetcher: HtmlFetcher | None = None):
        self.fetcher = fetcher or HttpxFetcher()
        self.fallback_fetcher = fallback_fetcher

    async def resolve(self, parsed: ParsedChannelUrl) -> ResolvedChannel:
        errors: list[ChannelResolutionError] = []
        for fetcher in [f for f in (self.fetcher, self.fallback_fetcher) if f is not None]:
            try:
                status, html = await fetcher.fetch(parsed.canonical_url)
            except ChannelResolutionError as exc:
                errors.append(exc)
                continue
            try:
                result = self._parse(parsed, status, html, fetcher is self.fallback_fetcher)
            except ChannelResolutionError as exc:
                # A blocked plain-HTTP fetch is often solved by rendering the
                # page, so keep trying the remaining fetchers.
                errors.append(exc)
                continue
            if result is not None:
                return result
            errors.append(
                ChannelResolutionError(
                    "Channel metadata was not present on the page.", "no_metadata"
                )
            )

        if parsed.kind == "channel_id":
            # A channel id is authoritative even when YouTube hides metadata
            # from this client; discovery will verify it later.
            return ResolvedChannel(
                channel_id=parsed.value,
                channel_name=None,
                handle=None,
                canonical_url=parsed.canonical_url,
                shorts_url=parsed.shorts_url,
                source="input",
                warnings=["Could not verify channel metadata; using the supplied channel ID."],
            )
        if errors:
            last = errors[-1]
            raise ChannelResolutionError(
                f"Could not resolve that channel URL. {last}", last.code
            )
        raise ChannelResolutionError(
            "Could not resolve that channel URL with the available fetchers.", "unresolved"
        )

    async def resolve_id(self, channel_id: str) -> ResolvedChannel:
        if not looks_like_channel_id(channel_id):
            raise ChannelResolutionError("Invalid channel ID.", "invalid_channel_id")
        canonical = f"https://www.youtube.com/channel/{channel_id}"
        parsed = ParsedChannelUrl(canonical, "channel_id", channel_id)
        try:
            status, html = await self.fetcher.fetch(canonical)
        except ChannelResolutionError as exc:
            raise exc
        result = self._parse(parsed, status, html, False)
        if result is not None:
            return result
        return ResolvedChannel(
            channel_id=channel_id,
            channel_name=None,
            handle=None,
            canonical_url=canonical,
            shorts_url=f"{canonical}/shorts",
            source="input",
            warnings=["Channel metadata could not be read."],
        )

    def _parse(
        self, parsed: ParsedChannelUrl, status: int, html: str, used_fallback: bool
    ) -> ResolvedChannel | None:
        lowered = html.lower()
        if status >= 400:
            if status == 404:
                raise ChannelResolutionError(
                    "That channel does not exist (YouTube returned 404).", "not_found"
                )
            raise ChannelResolutionError(
                f"YouTube returned HTTP {status} while resolving the channel.", "http_error"
            )

        # Only interpret page-level warnings when no channel metadata was
        # served: every YouTube page embeds strings like "recaptcha" and
        # consent copy in its configuration, which must not mask a valid page.
        channel_id = self._extract_channel_id(html)
        if not channel_id:
            if any(marker in lowered for marker in _CAPTCHA_MARKERS):
                raise ChannelResolutionError(
                    "YouTube presented a CAPTCHA / bot check; the channel could not be resolved.",
                    "captcha",
                )
            if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
                raise ChannelResolutionError(
                    "YouTube says that channel does not exist.", "not_found"
                )
            if any(marker in lowered for marker in _CONSENT_MARKERS):
                raise ChannelResolutionError(
                    "YouTube served a cookie-consent wall instead of the channel page.",
                    "consent_wall",
                )
            return None
        name = self._extract_name(html)
        handle = self._extract_handle(html)
        if parsed.kind == "handle" and not handle:
            handle = parsed.value
        canonical = (
            f"https://www.youtube.com/@{handle}"
            if parsed.kind == "handle" and handle
            else f"https://www.youtube.com/channel/{channel_id}"
        )
        return ResolvedChannel(
            channel_id=channel_id,
            channel_name=name,
            handle=handle,
            canonical_url=canonical,
            shorts_url=f"https://www.youtube.com/channel/{channel_id}/shorts",
            source="browser" if used_fallback else "html",
        )

    @staticmethod
    def _extract_channel_id(html: str) -> str | None:
        for pattern in (_CANONICAL_RE, _CANONICAL_RE_REVERSED, _CHANNEL_ID_JSON_RE, _EXTERNAL_ID_RE):
            match = pattern.search(html)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_name(html: str) -> str | None:
        match = _OG_TITLE_RE.search(html)
        name = _text(match.group(1)) if match else None
        if not name:
            match = _TITLE_RE.search(html)
            name = _text(match.group(1)) if match else None
        if name:
            for suffix in (" - YouTube", " – YouTube"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)].strip()
        return name

    @staticmethod
    def _extract_handle(html: str) -> str | None:
        match = _HANDLE_RE.search(html)
        if match:
            return match.group(1).lstrip("@")
        match = re.search(r'"canonicalBaseUrl":"/(@[A-Za-z0-9._-]+)"', html)
        if match:
            return match.group(1).lstrip("@")
        return None


__all__ = [
    "ChannelResolver",
    "ChannelResolutionError",
    "ResolvedChannel",
    "HttpxFetcher",
    "BrowserFetcher",
    "DESKTOP_UA",
    "CHANNEL_ID_RE",
]
