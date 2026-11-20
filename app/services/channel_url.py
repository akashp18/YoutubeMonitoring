from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,60}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,80}$")

_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
_RESERVED_FIRST_SEGMENTS = {
    "watch",
    "playlist",
    "results",
    "feed",
    "shorts",
    "embed",
    "v",
    "e",
    "attribution_link",
    "redirect",
    "signin",
    "account",
    "premium",
    "gaming",
    "music",
    "hashtag",
    "source",
}
_TAB_SEGMENTS = {
    "shorts",
    "videos",
    "streams",
    "featured",
    "playlists",
    "about",
    "community",
    "channels",
    "search",
    "live",
    "podcasts",
    "releases",
    "courses",
}


class ChannelUrlError(ValueError):
    """Raised when the supplied channel URL is unsupported or ambiguous."""

    def __init__(self, message: str, code: str = "invalid_url") -> None:
        super().__init__(message)
        self.code = code


class ParsedChannelUrl:
    """A validated channel URL before it is resolved to a numeric channel id."""

    __slots__ = ("input_url", "kind", "value", "canonical_url", "shorts_url")

    def __init__(self, input_url: str, kind: str, value: str) -> None:
        self.input_url = input_url
        self.kind = kind
        self.value = value
        self.canonical_url = _canonical_url(kind, value)
        self.shorts_url = f"{self.canonical_url}/shorts"

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"ParsedChannelUrl(kind={self.kind!r}, value={self.value!r})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_url": self.input_url,
            "kind": self.kind,
            "value": self.value,
            "canonical_url": self.canonical_url,
            "shorts_url": self.shorts_url,
        }


def _canonical_url(kind: str, value: str) -> str:
    if kind == "channel_id":
        return f"https://www.youtube.com/channel/{value}"
    if kind == "handle":
        return f"https://www.youtube.com/@{value}"
    if kind == "custom":
        return f"https://www.youtube.com/c/{value}"
    return f"https://www.youtube.com/user/{value}"


def parse_channel_url(raw: str) -> ParsedChannelUrl:
    """Validate and normalise supported YouTube channel URL formats.

    Supported: /@handle, /channel/UC..., /c/name, /user/name and legacy /name.
    Tab paths (/shorts, /videos, ...) and query strings are stripped.
    """
    if raw is None:
        raise ChannelUrlError("A channel URL is required.", "missing")
    text = raw.strip()
    if not text:
        raise ChannelUrlError("A channel URL is required.", "missing")
    if text.startswith("youtube.com") or text.startswith("www.youtube.com"):
        text = "https://" + text
    if "://" not in text:
        text = "https://" + text

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ChannelUrlError("Only http(s) URLs are supported.", "invalid_scheme")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ChannelUrlError(
            f"{host or 'That host'} is not a YouTube channel URL.", "invalid_host"
        )
    if host in {"music.youtube.com"}:
        raise ChannelUrlError(
            "YouTube Music URLs are not supported. Use youtube.com/@handle.", "invalid_host"
        )

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ChannelUrlError(
            "Use a channel URL such as https://www.youtube.com/@yourchannel.", "invalid_url"
        )

    first = segments[0]
    lowered = first.lower()

    if first.startswith("@"):
        handle = first[1:]
        if not HANDLE_RE.match(handle):
            raise ChannelUrlError(
                "That channel handle contains invalid characters.", "invalid_handle"
            )
        return ParsedChannelUrl(text, "handle", handle)

    if lowered == "channel":
        if len(segments) < 2:
            raise ChannelUrlError("A channel ID is missing after /channel/.", "invalid_url")
        channel_id = segments[1]
        if not CHANNEL_ID_RE.match(channel_id):
            raise ChannelUrlError(
                "A YouTube channel ID starts with 'UC' and has 24 characters.",
                "invalid_channel_id",
            )
        return ParsedChannelUrl(text, "channel_id", channel_id)

    if lowered == "c":
        if len(segments) < 2 or not USERNAME_RE.match(segments[1]):
            raise ChannelUrlError("A custom channel name is missing after /c/.", "invalid_url")
        return ParsedChannelUrl(text, "custom", segments[1])

    if lowered == "user":
        if len(segments) < 2 or not USERNAME_RE.match(segments[1]):
            raise ChannelUrlError("A username is missing after /user/.", "invalid_url")
        return ParsedChannelUrl(text, "user", segments[1])

    if lowered in _RESERVED_FIRST_SEGMENTS:
        raise ChannelUrlError(
            "That link is not a channel URL. Use a channel page URL such as "
            "https://www.youtube.com/@yourchannel.",
            "not_a_channel",
        )

    # Legacy youtube.com/SomeName vanity URL, optionally followed by a tab.
    if len(segments) == 1 or segments[1].lower() in _TAB_SEGMENTS:
        if not USERNAME_RE.match(first):
            raise ChannelUrlError("That URL is not a valid channel URL.", "invalid_url")
        return ParsedChannelUrl(text, "custom", first)

    raise ChannelUrlError(
        "That YouTube link does not point at a channel page.", "not_a_channel"
    )


def looks_like_channel_id(value: str | None) -> bool:
    return bool(value and CHANNEL_ID_RE.match(value.strip()))


def normalize_channel_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned if CHANNEL_ID_RE.match(cleaned) else None
