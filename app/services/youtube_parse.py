"""Robust, selector-free parsing of YouTube page payloads.

YouTube reshuffles renderer names and CSS classes regularly, so discovery reads
the embedded JSON (`ytInitialData`, innertube responses) and only falls back to
DOM anchors for anything new. These helpers are pure functions and unit tested.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHORTS_HREF_RE = re.compile(r"/shorts/([A-Za-z0-9_-]{6,})")

SHORT_RENDERER_KEYS = {
    "reelItemRenderer",
    "shortsLockupViewModel",
    "gridVideoRenderer",
    "compactVideoRenderer",
    "videoRenderer",
    "richItemRenderer",
    "lockupViewModel",
}
# Renderers that are only ever Shorts.
SHORTS_ONLY_RENDERER_KEYS = {"reelItemRenderer", "shortsLockupViewModel"}

_COUNT_WORDS = r"(?:[KMB]|thousand|million|billion)?"
_VIEW_SUFFIX_RE = re.compile(
    r"[\s,]*(?:[-–—]\s*)?(?:[\d.,]+\s*" + _COUNT_WORDS + r"\s*views?"
    r"|[\d.,]+\s*" + _COUNT_WORDS + r"\s*watching)"
    r"\s*(?:[-–—]\s*play Short)?$",
    re.IGNORECASE,
)
_PLAY_SHORT_RE = re.compile(r"\s*(?:[-–—]\s*)?play Short\s*$", re.IGNORECASE)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def clean_title(value: Any) -> str | None:
    text = _norm(value)
    if not text:
        return None
    text = _PLAY_SHORT_RE.sub("", text)
    text = _VIEW_SUFFIX_RE.sub("", text)
    text = re.sub(r"^(?:Shorts\s*[-–—]\s*)", "", text, flags=re.IGNORECASE)
    return _norm(text)


def parse_duration_text(value: Any) -> int | None:
    text = _norm(value)
    if not text:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if not match:
        return None
    parts = [int(part) for part in match.groups() if part is not None]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _walk(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _first_video_id(node: Any) -> str | None:
    for child in _walk(node):
        if not isinstance(child, dict):
            continue
        for key in ("reelWatchEndpoint", "watchEndpoint", "watchEndpointSupportedOnesieConfig"):
            endpoint = child.get(key)
            if isinstance(endpoint, dict):
                video_id = endpoint.get("videoId")
                if isinstance(video_id, str) and VIDEO_ID_RE.match(video_id):
                    return video_id
        for key in ("videoId", "video_id"):
            value = child.get(key)
            if isinstance(value, str) and VIDEO_ID_RE.match(value):
                return value
    return None


def _first_title(node: Any) -> str | None:
    candidates: list[Any] = []

    headline = node.get("headline")
    if isinstance(headline, dict):
        candidates.append(headline.get("simpleText"))
        runs = headline.get("runs")
        if isinstance(runs, list):
            candidates.append(
                "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict))
            )

    overlay = node.get("overlayMetadata")
    if isinstance(overlay, dict):
        primary = overlay.get("primaryText")
        if isinstance(primary, dict):
            candidates.append(primary.get("content"))
        secondary = overlay.get("secondaryText")
        if isinstance(secondary, dict):
            candidates.append(secondary.get("content"))

    title = node.get("title")
    if isinstance(title, dict):
        candidates.append(title.get("simpleText"))
        runs = title.get("runs")
        if isinstance(runs, list):
            candidates.append(
                "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict))
            )
    elif isinstance(title, str):
        candidates.append(title)

    # Current YouTube Shorts shelves put the title only in accessibilityText:
    # "Can I Beat Magnus Carlsen?, 1.1 million views - play Short".
    candidates.append(node.get("accessibilityText"))

    accessibility = node.get("accessibility")
    if isinstance(accessibility, dict):
        data = accessibility.get("accessibilityData")
        if isinstance(data, dict):
            candidates.append(data.get("label"))

    metadata = node.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("title"), dict):
        candidates.append(metadata["title"].get("simpleText"))

    for candidate in candidates:
        cleaned = clean_title(candidate)
        if cleaned and len(cleaned) > 1:
            return cleaned
    return None


def _first_thumbnail(node: Any) -> str | None:
    best: tuple[int, str] | None = None
    for child in _walk(node):
        if not isinstance(child, dict):
            continue
        sources = child.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                url = source.get("url")
                if not isinstance(url, str) or not url.startswith("http"):
                    continue
                width = source.get("width")
                width = int(width) if isinstance(width, (int, float)) else 0
                if best is None or width > best[0]:
                    best = (width, url)
        thumbnails = child.get("thumbnails")
        if isinstance(thumbnails, list) and thumbnails:
            last = thumbnails[-1]
            if isinstance(last, dict) and isinstance(last.get("url"), str):
                width = last.get("width")
                width = int(width) if isinstance(width, (int, float)) else 0
                if best is None or width > best[0]:
                    best = (width, last["url"])
    return best[1] if best else None


def _duration_seconds(node: Any) -> int | None:
    for child in _walk(node):
        if not isinstance(child, dict):
            continue
        text = child.get("lengthText")
        if isinstance(text, dict):
            seconds = parse_duration_text(text.get("simpleText"))
            if seconds:
                return seconds
        elif isinstance(text, str):
            seconds = parse_duration_text(text)
            if seconds:
                return seconds
        simple = child.get("simpleText")
        if isinstance(simple, str) and re.fullmatch(r"\d{1,2}:\d{2}", simple):
            seconds = parse_duration_text(simple)
            if seconds:
                return seconds
        if "durationSeconds" in child:
            try:
                return int(child["durationSeconds"])
            except (TypeError, ValueError):
                continue
    return None


def _has_shorts_marker(node: Any) -> bool:
    for child in _walk(node):
        if isinstance(child, dict):
            for key, value in child.items():
                if key in {"url", "canonicalBaseUrl", "webCommandMetadataUrl"} and isinstance(
                    value, str
                ) and "/shorts/" in value:
                    return True
                if key in {"thumbnailOverlayTimeStatusRenderer", "thumbnailOverlayTimeStatus"}:
                    blob = json.dumps(value)
                    if '"SHORTS"' in blob or "SHORT" in blob:
                        return True
    return False


def iter_short_nodes(data: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (renderer_key, node) pairs for nodes that represent Shorts."""
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key not in SHORT_RENDERER_KEYS or not isinstance(value, dict):
                continue
            if key in SHORTS_ONLY_RENDERER_KEYS or _has_shorts_marker(value):
                yield key, value


def iter_shorts_from_data(data: Any) -> Iterator[dict[str, Any]]:
    """Extract unique Shorts records from any YouTube JSON payload.

    The same video can appear both as an outer richItemRenderer and as its
    inner shortsLockupViewModel. Metadata is merged by video id so a container
    node without a title cannot hide the title held by the specific renderer.
    """
    if not data:
        return
    collected: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for _key, node in iter_short_nodes(data):
        video_id = _first_video_id(node)
        if not video_id:
            continue
        title = _first_title(node)
        thumbnail = _first_thumbnail(node)
        duration = _duration_seconds(node)
        existing = collected.get(video_id)
        if existing is None:
            collected[video_id] = {
                "video_id": video_id,
                "title": title,
                "thumbnail_url": thumbnail
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "video_url": f"https://www.youtube.com/shorts/{video_id}",
                "duration_seconds": duration,
            }
            order.append(video_id)
        else:
            existing["title"] = existing["title"] or title
            existing["thumbnail_url"] = existing["thumbnail_url"] or thumbnail
            existing["duration_seconds"] = existing["duration_seconds"] or duration
    for video_id in order:
        yield collected[video_id]


def _extract_balanced_json(text: str, start_index: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return None


def extract_json_after_marker(html: str, markers: Iterable[str]) -> Any | None:
    """Find `marker {...}` in an HTML page and decode the JSON object."""
    for marker in markers:
        position = html.find(marker)
        while position != -1:
            brace = html.find("{", position + len(marker))
            if brace == -1:
                break
            gap = html[position + len(marker) : brace]
            if len(gap) <= 40 and gap.strip() in {"", "=", ":", "=:", " ="}:
                blob = _extract_balanced_json(html, brace)
                if blob:
                    try:
                        return json.loads(blob)
                    except ValueError:
                        pass
            position = html.find(marker, position + 1)
    return None


def extract_initial_data(html: str) -> Any | None:
    return extract_json_after_marker(
        html, ("var ytInitialData = ", "ytInitialData = ", '"ytInitialData":')
    )


def extract_initial_player_response(html: str) -> Any | None:
    return extract_json_after_marker(
        html,
        (
            "var ytInitialPlayerResponse = ",
            "ytInitialPlayerResponse = ",
            '"ytInitialPlayerResponse":',
        ),
    )


def parse_shorts_from_html(html: str) -> list[dict[str, Any]]:
    """HTML-only discovery used when Playwright is unavailable."""
    data = extract_initial_data(html)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if data:
        for item in iter_shorts_from_data(data):
            if item["video_id"] not in seen:
                seen.add(item["video_id"])
                items.append(item)
    if items:
        return items
    # Last resort: scan raw HTML for /shorts/<id> links.
    for match in SHORTS_HREF_RE.finditer(html):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        items.append(
            {
                "video_id": video_id,
                "title": None,
                "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "video_url": f"https://www.youtube.com/shorts/{video_id}",
                "duration_seconds": None,
            }
        )
    return items
