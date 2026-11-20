from __future__ import annotations

import pytest

from app.services.channel_url import ChannelUrlError, normalize_channel_id, parse_channel_url

CHANNEL_ID = "UCabcdefghijklmnopqrstuv"


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("https://www.youtube.com/@mkbhd", "handle", "mkbhd"),
        ("https://www.youtube.com/@mkbhd/", "handle", "mkbhd"),
        ("https://www.youtube.com/@mkbhd/shorts", "handle", "mkbhd"),
        ("youtube.com/@mkbhd?sub_confirmation=1", "handle", "mkbhd"),
        ("m.youtube.com/@mkbhd", "handle", "mkbhd"),
        (f"https://www.youtube.com/channel/{CHANNEL_ID}", "channel_id", CHANNEL_ID),
        (f"https://www.youtube.com/channel/{CHANNEL_ID}/shorts", "channel_id", CHANNEL_ID),
        ("https://www.youtube.com/c/SomeChannel", "custom", "SomeChannel"),
        ("https://www.youtube.com/user/legacyuser", "user", "legacyuser"),
        ("https://www.youtube.com/LegacyVanity", "custom", "LegacyVanity"),
        ("https://www.youtube.com/LegacyVanity/videos", "custom", "LegacyVanity"),
    ],
)
def test_parses_supported_channel_urls(raw: str, kind: str, value: str) -> None:
    parsed = parse_channel_url(raw)
    assert parsed.kind == kind
    assert parsed.value == value
    assert parsed.canonical_url.startswith("https://www.youtube.com/")
    assert parsed.shorts_url.endswith("/shorts")


@pytest.mark.parametrize(
    "raw,code",
    [
        ("", "missing"),
        ("   ", "missing"),
        ("https://vimeo.com/@someone", "invalid_host"),
        ("https://youtu.be/abcdefghijk", "invalid_host"),
        ("https://music.youtube.com/@someone", "invalid_host"),
        ("https://www.youtube.com/", "invalid_url"),
        ("https://www.youtube.com/watch?v=abcdefghijk", "not_a_channel"),
        ("https://www.youtube.com/shorts/abcdefghijk", "not_a_channel"),
        ("https://www.youtube.com/playlist?list=PL123", "not_a_channel"),
        ("https://www.youtube.com/channel/NOT_A_CHANNEL_ID", "invalid_channel_id"),
        ("https://www.youtube.com/channel/", "invalid_url"),
        ("https://www.youtube.com/@bad handle", "invalid_handle"),
        ("ftp://www.youtube.com/@someone", "invalid_scheme"),
    ],
)
def test_rejects_invalid_urls(raw: str, code: str) -> None:
    with pytest.raises(ChannelUrlError) as excinfo:
        parse_channel_url(raw)
    assert excinfo.value.code == code


def test_channel_id_normalisation() -> None:
    assert normalize_channel_id(CHANNEL_ID) == CHANNEL_ID
    assert normalize_channel_id("  " + CHANNEL_ID + "  ") == CHANNEL_ID
    assert normalize_channel_id("UCtoolong" + "x" * 30) is None
    assert normalize_channel_id(None) is None
