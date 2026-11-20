from __future__ import annotations

import json

import pytest

from app.domain import DiscoverySource
from app.services.shorts_discovery import DiscoveryError, ShortsDiscoveryService
from app.services.youtube_api import YouTubeAPIError
from app.services.youtube_parse import (
    clean_title,
    extract_initial_data,
    iter_shorts_from_data,
    parse_duration_text,
    parse_shorts_from_html,
)
from tests.fakes import (
    FakeDiscoverySession,
    FakeProbe,
    FakeYouTubeAPI,
    FakeHtmlFetcher,
)

CHANNEL = {
    "id": 1,
    "channel_id": "UCabcdefghijklmnopqrstuv",
    "channel_name": "Example Channel",
    "shorts_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/shorts",
}


def short_item(video_id: str, title: str | None = None) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def initial_data_payload() -> dict:
    return {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {
                                "richGridRenderer": {
                                    "contents": [
                                        {
                                            "richItemRenderer": {
                                                "content": {
                                                    "shortsLockupViewModel": {
                                                        "entityId": "shorts-shelf-item-1",
                                                        "onTap": {
                                                            "innertubeCommand": {
                                                                "reelWatchEndpoint": {
                                                                    "videoId": "aaaaaaaaaaa"
                                                                }
                                                            }
                                                        },
                                                        "overlayMetadata": {
                                                            "primaryText": {"content": "First short"},
                                                            "secondaryText": {"content": "1.2M views"},
                                                        },
                                                        "thumbnail": {
                                                            "sources": [
                                                                {
                                                                    "url": "https://i.ytimg.com/vi/aaaaaaaaaaa/hq1.jpg",
                                                                    "width": 480,
                                                                    "height": 360,
                                                                },
                                                                {
                                                                    "url": "https://i.ytimg.com/vi/aaaaaaaaaaa/hq2.jpg",
                                                                    "width": 720,
                                                                    "height": 540,
                                                                },
                                                            ]
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            "richItemRenderer": {
                                                "content": {
                                                    "shortsLockupViewModel": {
                                                        "entityId": "shorts-shelf-item-2",
                                                        "onTap": {
                                                            "innertubeCommand": {
                                                                "reelWatchEndpoint": {
                                                                    "videoId": "bbbbbbbbbbb"
                                                                }
                                                            }
                                                        },
                                                        "overlayMetadata": {
                                                            "primaryText": {"content": "Second short"}
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            # duplicate id, must be ignored
                                            "richItemRenderer": {
                                                "content": {
                                                    "shortsLockupViewModel": {
                                                        "onTap": {
                                                            "innertubeCommand": {
                                                                "reelWatchEndpoint": {
                                                                    "videoId": "aaaaaaaaaaa"
                                                                }
                                                            }
                                                        },
                                                        "overlayMetadata": {
                                                            "primaryText": {"content": "Duplicate"}
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            # regular horizontal upload - must be excluded
                                            "richItemRenderer": {
                                                "content": {
                                                    "videoRenderer": {
                                                        "videoId": "ccccccccccc",
                                                        "title": {"simpleText": "A normal video"},
                                                        "navigationEndpoint": {
                                                            "watchEndpoint": {
                                                                "videoId": "ccccccccccc"
                                                            }
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    ]
                                }
                            }
                        }
                    }
                ]
            }
        }
    }


def test_parses_shorts_and_deduplicates() -> None:
    items = list(iter_shorts_from_data(initial_data_payload()))
    assert [item["video_id"] for item in items] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert items[0]["title"] == "First short"
    assert items[0]["thumbnail_url"].endswith("hq2.jpg")
    assert items[1]["video_url"] == "https://www.youtube.com/shorts/bbbbbbbbbbb"


def test_merges_title_from_nested_accessibility_text() -> None:
    """Real channel payloads wrap the new renderer and only carry the title
    inside it, as accessibilityText."""
    data = {
        "contents": [
            {
                "richItemRenderer": {
                    "content": {
                        "shortsLockupViewModel": {
                            "entityId": "shorts-shelf-item-WjCH5JUNzao",
                            "accessibilityText": "Can I Beat Magnus Carlsen?, "
                            "1.1 million views - play Short",
                            "onTap": {
                                "innertubeCommand": {
                                    "commandMetadata": {
                                        "webCommandMetadata": {"url": "/shorts/WjCH5JUNzao"}
                                    },
                                    "reelWatchEndpoint": {"videoId": "WjCH5JUNzao"},
                                }
                            },
                        }
                    }
                }
            }
        ]
    }
    items = list(iter_shorts_from_data(data))
    assert len(items) == 1
    assert items[0]["title"] == "Can I Beat Magnus Carlsen?"
    assert items[0]["video_id"] == "WjCH5JUNzao"


def test_ignores_regular_uploads() -> None:
    data = initial_data_payload()
    grid = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["richGridRenderer"]["contents"]
    # Remove the two genuine shorts, leaving only the regular upload.
    grid[:] = [grid[3]]
    assert list(iter_shorts_from_data(data)) == []


def test_extract_initial_data_handles_braces_in_strings() -> None:
    payload = {"contents": {"note": "a { brace } inside a string", "items": [1, 2]}}
    html = f"<script>var ytInitialData = {json.dumps(payload)};</script>"
    assert extract_initial_data(html) == payload


def test_html_fallback_scans_shorts_links() -> None:
    html = (
        "<html><body>"
        '<a href="/shorts/ddddddddddd">one</a>'
        '<a href="/shorts/eeeeeeeeeee">two</a>'
        '<a href="/shorts/ddddddddddd">dup</a>'
        "</body></html>"
    )
    items = parse_shorts_from_html(html)
    assert {item["video_id"] for item in items} == {"ddddddddddd", "eeeeeeeeeee"}


def test_title_and_duration_helpers() -> None:
    assert clean_title("My Short, 1.2M views - play Short") == "My Short"
    assert clean_title("Another one - play Short") == "Another one"
    assert parse_duration_text("0:45") == 45
    assert parse_duration_text("1:02:03") == 3723
    assert parse_duration_text("not a duration") is None


@pytest.mark.asyncio
async def test_api_discovery_filters_long_videos_and_non_shorts() -> None:
    api = FakeYouTubeAPI(
        playlist=[
            short_item("aaaaaaaaaaa", "Short A"),
            short_item("bbbbbbbbbbb", "Short B"),
            short_item("ccccccccccc", "Long"),
        ],
        details=[
            {"video_id": "aaaaaaaaaaa", "title": "Short A", "duration_seconds": 45},
            {"video_id": "bbbbbbbbbbb", "title": "Short B", "duration_seconds": 120},
            {"video_id": "ccccccccccc", "title": "Long", "duration_seconds": 600},
        ],
    )
    probe = FakeProbe({"aaaaaaaaaaa": True, "bbbbbbbbbbb": False})
    service = ShortsDiscoveryService(api=api, probe=probe)
    result = await service.discover(CHANNEL)
    ids = [item["video_id"] for item in result.items]
    assert ids == ["aaaaaaaaaaa"]
    assert result.source == DiscoverySource.API
    assert probe.calls == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


@pytest.mark.asyncio
async def test_api_failure_falls_back_to_browser() -> None:
    class BrokenAPI(FakeYouTubeAPI):
        async def channel(self, channel_id=None, handle=None):
            raise YouTubeAPIError("quota exceeded", "quota")

    session = FakeDiscoverySession(
        [
            {
                "items": [short_item("aaaaaaaaaaa", "Short A")],
                "scrollHeight": 800,
                "scrollY": 0,
                "viewport": 800,
            }
        ]
    )

    class Browser:
        async def open_discovery_session(self):
            return session

    service = ShortsDiscoveryService(browser_manager=Browser(), api=BrokenAPI([]))
    result = await service.discover(CHANNEL)
    assert result.source == DiscoverySource.BROWSER
    assert [item["video_id"] for item in result.items] == ["aaaaaaaaaaa"]
    assert any("quota" in note for note in result.notes)


@pytest.mark.asyncio
async def test_browser_discovery_scrolls_and_deduplicates() -> None:
    session = FakeDiscoverySession(
        [
            {
                "items": [short_item("aaaaaaaaaaa", "A"), short_item("bbbbbbbbbbb", "B")],
                "scrollHeight": 2000,
                "scrollY": 0,
                "viewport": 800,
            },
            {
                "items": [short_item("aaaaaaaaaaa", "A"), short_item("ccccccccccc", "C")],
                "scrollHeight": 2000,
                "scrollY": 800,
                "viewport": 800,
            },
            {
                "items": [],
                "scrollHeight": 1600,
                "scrollY": 1600,
                "viewport": 800,
            },
            {
                "items": [],
                "scrollHeight": 1600,
                "scrollY": 1600,
                "viewport": 800,
            },
        ]
    )

    class Browser:
        async def open_discovery_session(self):
            return session

    service = ShortsDiscoveryService(
        browser_manager=Browser(), max_scrolls=8, scroll_delay_ms=200, page_timeout_ms=5000
    )
    result = await service.discover(CHANNEL)
    assert result.source == DiscoverySource.BROWSER
    assert [item["video_id"] for item in result.items] == [
        "aaaaaaaaaaa",
        "bbbbbbbbbbb",
        "ccccccccccc",
    ]
    assert result.truncated is False
    assert session.opened_url.endswith("/shorts")
    assert session.scrolled > 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_browser_discovery_flags_truncation_at_scroll_limit() -> None:
    snapshot = {
        "items": [short_item("aaaaaaaaaaa", "A")],
        "scrollHeight": 100000,
        "scrollY": 0,
        "viewport": 800,
    }
    session = FakeDiscoverySession([snapshot])

    class Browser:
        async def open_discovery_session(self):
            return session

    service = ShortsDiscoveryService(
        browser_manager=Browser(), max_scrolls=2, scroll_delay_ms=200, page_timeout_ms=5000
    )
    result = await service.discover(CHANNEL)
    assert result.truncated is True


@pytest.mark.asyncio
async def test_empty_browser_result_raises() -> None:
    session = FakeDiscoverySession([])
    session.snapshots = [{"items": [], "scrollHeight": 800, "scrollY": 800, "viewport": 800}]

    class Browser:
        async def open_discovery_session(self):
            return session

    service = ShortsDiscoveryService(browser_manager=Browser(), max_scrolls=1)
    with pytest.raises(DiscoveryError):
        await service.discover(CHANNEL)


@pytest.mark.asyncio
async def test_html_discovery_fallback_is_flagged_truncated() -> None:
    payload = {"contents": initial_data_payload()["contents"]}
    html = f"<html><script>var ytInitialData = {json.dumps(payload)};</script></html>"
    fetcher = FakeHtmlFetcher(
        {"https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/shorts": (200, html)}
    )
    service = ShortsDiscoveryService(fetcher=fetcher)
    result = await service.discover(CHANNEL)
    assert result.source == DiscoverySource.HTML
    assert result.truncated is True
    assert [item["video_id"] for item in result.items] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
