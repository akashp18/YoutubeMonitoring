"""Opt-in integration tests against real public YouTube pages.

These are skipped by default: they depend on YouTube being reachable from the
machine and on YouTube not blocking automated access, and they never generate
engagement. Run them with:

    YT_LIVE_TESTS=1 YT_LIVE_CHANNEL_URL=https://www.youtube.com/@somechannel \
        python -m pytest tests/test_live_youtube.py -v
"""

from __future__ import annotations

import os

import pytest

from app.services.channel_resolver import ChannelResolver, HttpxFetcher
from app.services.channel_url import parse_channel_url

LIVE = bool(os.environ.get("YT_LIVE_TESTS"))
CHANNEL_URL = os.environ.get("YT_LIVE_CHANNEL_URL", "")

pytestmark = pytest.mark.skipif(
    not LIVE or not CHANNEL_URL,
    reason="live tests disabled (set YT_LIVE_TESTS=1 and YT_LIVE_CHANNEL_URL)",
)


@pytest.mark.asyncio
async def test_live_channel_resolution() -> None:
    resolver = ChannelResolver(fetcher=HttpxFetcher())
    resolved = await resolver.resolve(parse_channel_url(CHANNEL_URL))
    assert resolved.channel_id.startswith("UC")
    assert len(resolved.channel_id) == 24


@pytest.mark.asyncio
async def test_live_playwright_discovery_finds_shorts() -> None:
    pytest.importorskip("playwright")
    from app.config import get_settings
    from app.services.browser import BrowserManager
    from app.services.shorts_discovery import ShortsDiscoveryService
    from app.settings_store import SettingsStore
    from app.db import Database

    settings = get_settings()
    db = Database(settings.database_path)
    db.migrate()
    store = SettingsStore(db)
    browser = BrowserManager(store)
    try:
        resolver = ChannelResolver(fetcher=HttpxFetcher())
        resolved = await resolver.resolve(parse_channel_url(CHANNEL_URL))
        service = ShortsDiscoveryService(
            browser_manager=browser, max_scrolls=3, scroll_delay_ms=800
        )
        result = await service.discover(
            {
                "channel_id": resolved.channel_id,
                "channel_name": resolved.channel_name,
                "shorts_url": resolved.shorts_url,
            }
        )
        assert isinstance(result.items, list)
        for item in result.items:
            assert len(item["video_id"]) == 11
    finally:
        await browser.close()
