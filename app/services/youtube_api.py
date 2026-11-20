"""Minimal YouTube Data API v3 client used for reliable Shorts discovery."""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.services.youtube_parse import parse_duration_text

API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    def __init__(self, message: str, code: str = "api_error") -> None:
        super().__init__(message)
        self.code = code


def _iso8601_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(
        r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$",
        value,
    )
    if not match or not any(match.groupdict().values()):
        return parse_duration_text(value)
    parts = {key: int(val or 0) for key, val in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


class YouTubeDataAPI:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, timeout: float = 25.0):
        if not api_key:
            raise YouTubeAPIError("No YouTube API key configured", "missing_key")
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "key": self.api_key}
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise YouTubeAPIError(f"YouTube API unreachable: {exc}", "network") from exc

        if response.status_code == 429:
            raise YouTubeAPIError("YouTube API rate limit reached", "rate_limited")
        if response.status_code >= 400:
            reason = ""
            try:
                error = response.json().get("error", {})
                errors = error.get("errors") or []
                if errors:
                    reason = str(errors[0].get("reason") or "")
            except ValueError:
                pass
            if reason in {"quotaExceeded", "dailyLimitExceeded"}:
                raise YouTubeAPIError("YouTube API quota exceeded", "quota")
            if reason in {"keyInvalid", "ipRefererBlocked"}:
                raise YouTubeAPIError("The YouTube API key is not valid for this request", "auth")
            raise YouTubeAPIError(
                f"YouTube API error {response.status_code}: {reason or response.text[:200]}",
                "api_error",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise YouTubeAPIError("YouTube API returned invalid JSON", "api_error") from exc

    async def channel(self, channel_id: str | None = None, handle: str | None = None) -> dict[str, Any] | None:
        params = {"part": "snippet,contentDetails"}
        if channel_id:
            params["id"] = channel_id
        elif handle:
            params["forHandle"] = handle if handle.startswith("@") else f"@{handle}"
        else:
            raise YouTubeAPIError("channel id or handle required", "bad_request")
        data = await self._get("channels", params)
        items = data.get("items") or []
        return items[0] if items else None

    async def playlist_videos(self, playlist_id: str, max_videos: int = 1000) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(collected) < max_videos:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("playlistItems", params)
            for item in data.get("items") or []:
                snippet = item.get("snippet") or {}
                content = item.get("contentDetails") or {}
                video_id = content.get("videoId") or snippet.get("resourceId", {}).get("videoId")
                if not video_id:
                    continue
                collected.append(
                    {
                        "video_id": video_id,
                        "title": snippet.get("title"),
                        "thumbnail_url": (snippet.get("thumbnails") or {})
                        .get("high", {})
                        .get("url"),
                        "video_url": f"https://www.youtube.com/shorts/{video_id}",
                        "published_at": (snippet.get("publishedAt") or content.get("videoPublishedAt")),
                    }
                )
                if len(collected) >= max_videos:
                    break
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return collected

    async def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index in range(0, len(video_ids), 50):
            chunk = video_ids[index : index + 50]
            data = await self._get(
                "videos", {"part": "snippet,contentDetails,status", "id": ",".join(chunk)}
            )
            for item in data.get("items") or []:
                snippet = item.get("snippet") or {}
                details = item.get("contentDetails") or {}
                results.append(
                    {
                        "video_id": item.get("id"),
                        "title": snippet.get("title"),
                        "thumbnail_url": (snippet.get("thumbnails") or {})
                        .get("high", {})
                        .get("url"),
                        "duration_seconds": _iso8601_duration_seconds(details.get("duration")),
                        "video_url": f"https://www.youtube.com/shorts/{item.get('id')}",
                        "privacy_status": (item.get("status") or {}).get("privacyStatus"),
                    }
                )
        return results

    async def latest_videos(self, channel_id: str, max_videos: int = 50) -> list[dict[str, Any]]:
        channel = await self.channel(channel_id=channel_id)
        if not channel:
            raise YouTubeAPIError("Channel not found", "not_found")
        uploads = (
            (channel.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
        )
        if not uploads:
            raise YouTubeAPIError("Channel has no uploads playlist", "not_found")
        return await self.playlist_videos(uploads, max_videos=max_videos)
