from __future__ import annotations

import asyncio

import pytest

from app.services.channel_resolver import (
    BrowserFetcher,
    ChannelResolutionError,
    ChannelResolver,
    ResolvedChannel,
)
from app.services.channel_url import parse_channel_url
from tests.fakes import FakeHtmlFetcher

CHANNEL_ID = "UCabcdefghijklmnopqrstuv"

CHANNEL_HTML = f"""
<!doctype html><html><head>
<title>Example Channel - YouTube</title>
<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">
<meta property="og:title" content="Example Channel">
</head><body>
<script>var ytInitialData = {{"metadata":{{"channelMetadataRenderer":{{"title":"Example Channel"}}}}}};</script>
"vanityChannelUrl":"https://www.youtube.com/@examplechannel"
</body></html>
"""


def resolver_with(status: int, html: str, fallback: FakeHtmlFetcher | None = None) -> ChannelResolver:
    return ChannelResolver(fetcher=FakeHtmlFetcher({}), fallback_fetcher=fallback)


@pytest.mark.asyncio
async def test_resolves_handle_to_channel_id() -> None:
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@examplechannel": (200, CHANNEL_HTML)})
    resolver = ChannelResolver(fetcher=fetcher)
    resolved = await resolver.resolve(parse_channel_url("https://www.youtube.com/@examplechannel"))
    assert isinstance(resolved, ResolvedChannel)
    assert resolved.channel_id == CHANNEL_ID
    assert resolved.channel_name == "Example Channel"
    assert resolved.handle == "examplechannel"
    assert resolved.shorts_url.endswith("/shorts")


@pytest.mark.asyncio
async def test_missing_channel_reports_not_found() -> None:
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@gone": (404, "gone")})
    resolver = ChannelResolver(fetcher=fetcher)
    with pytest.raises(ChannelResolutionError) as excinfo:
        await resolver.resolve(parse_channel_url("https://www.youtube.com/@gone"))
    assert excinfo.value.code == "not_found"


@pytest.mark.asyncio
async def test_captcha_is_reported_not_bypassed() -> None:
    html = "<html><body>Please confirm you're not a bot. unusual traffic</body></html>"
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@blocked": (200, html)})
    resolver = ChannelResolver(fetcher=fetcher)
    with pytest.raises(ChannelResolutionError) as excinfo:
        await resolver.resolve(parse_channel_url("https://www.youtube.com/@blocked"))
    assert excinfo.value.code == "captcha"


@pytest.mark.asyncio
async def test_consent_wall_is_reported() -> None:
    html = "<html><body>Before you continue to YouTube. We use cookies and data.</body></html>"
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@consent": (200, html)})
    resolver = ChannelResolver(fetcher=fetcher)
    with pytest.raises(ChannelResolutionError) as excinfo:
        await resolver.resolve(parse_channel_url("https://www.youtube.com/@consent"))
    assert excinfo.value.code == "consent_wall"


@pytest.mark.asyncio
async def test_falls_back_to_second_fetcher() -> None:
    primary = FakeHtmlFetcher({})  # 404
    fallback = FakeHtmlFetcher({"https://www.youtube.com/@examplechannel": (200, CHANNEL_HTML)})
    resolver = ChannelResolver(fetcher=primary, fallback_fetcher=fallback)
    resolved = await resolver.resolve(parse_channel_url("https://www.youtube.com/@examplechannel"))
    assert resolved.channel_id == CHANNEL_ID
    assert resolved.source == "browser"


@pytest.mark.asyncio
async def test_supplied_channel_id_is_authoritative_when_metadata_blocked() -> None:
    fetcher = FakeHtmlFetcher(
        {f"https://www.youtube.com/channel/{CHANNEL_ID}": (200, "<html>no metadata</html>")}
    )
    resolver = ChannelResolver(fetcher=fetcher)
    resolved = await resolver.resolve(parse_channel_url(f"https://www.youtube.com/channel/{CHANNEL_ID}"))
    assert resolved.channel_id == CHANNEL_ID
    assert resolved.warnings


@pytest.mark.asyncio
async def test_captcha_config_strings_do_not_mask_valid_metadata() -> None:
    extra = (
        '<script>var cfg = {"recaptchaEnabled": true, '
        '"consentCopy": "We use cookies and data"};</script></head>'
    )
    html = CHANNEL_HTML.replace("</head>", extra)
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@examplechannel": (200, html)})
    resolver = ChannelResolver(fetcher=fetcher)
    resolved = await resolver.resolve(parse_channel_url("https://www.youtube.com/@examplechannel"))
    assert resolved.channel_id == CHANNEL_ID


@pytest.mark.asyncio
async def test_browser_fetcher_uses_page_adapter_interface(monkeypatch) -> None:
    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    class Adapter:
        async def goto(self, url: str, timeout_ms: int) -> int:
            return 200

        async def evaluate(self, expression: str):
            return "<html>rendered channel page</html>"

        async def close(self) -> None:
            return None

    class Manager:
        async def new_page(self) -> Adapter:
            return Adapter()

    status, html = await BrowserFetcher(Manager()).fetch("https://www.youtube.com/@examplechannel")
    assert status == 200
    assert "rendered channel page" in html


@pytest.mark.asyncio
async def test_handle_without_metadata_is_unresolved() -> None:
    fetcher = FakeHtmlFetcher({"https://www.youtube.com/@ghost": (200, "<html>nothing</html>")})
    resolver = ChannelResolver(fetcher=fetcher)
    with pytest.raises(ChannelResolutionError):
        await resolver.resolve(parse_channel_url("https://www.youtube.com/@ghost"))
