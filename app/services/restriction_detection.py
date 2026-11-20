"""RestrictionDetectionService.

Maps observable YouTube page signals onto the monitor's status vocabulary.

Priority is deliberate: automation challenges and consent walls are reported as
INCONCLUSIVE (never as a restriction of the video), explicit player messages
win over loose page text, and page text is only consulted when no playable
video exists so that a playing Short can never be called "unavailable" because
of unrelated text on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.domain import Availability, RestrictionType


@dataclass(frozen=True)
class Verdict:
    verdict: str  # ACCESSIBLE | RESTRICTED | FAILED | INCONCLUSIVE
    restriction_type: str
    message: str
    confidence: str = "explicit"
    matched: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    restriction_type: str
    verdict: str
    message: str


def _p(pattern: str, restriction_type: str, verdict: str, message: str) -> _Pattern:
    return _Pattern(re.compile(pattern, re.IGNORECASE | re.DOTALL), restriction_type, verdict, message)


CAPTCHA_PATTERN = _p(
    r"(confirm you(?:'|’)?re not a bot|unusual traffic|automated queries|"
    r"verify (?:that )?you(?:'|’)?re (?:a )?human|recaptcha|too many requests from your network)",
    RestrictionType.CAPTCHA,
    "INCONCLUSIVE",
    "YouTube presented an automated-access challenge (CAPTCHA / bot check). "
    "The check could not be completed and was not retried aggressively.",
)

PATTERNS: list[_Pattern] = [
    CAPTCHA_PATTERN,
    _p(
        r"(sign in to confirm your age|confirm your age|age-restricted|"
        r"may be inappropriate for some users|inappropriate for some users)",
        RestrictionType.AGE_RESTRICTED,
        "RESTRICTED",
        "The video is age-restricted and requires a signed-in viewer to confirm their age.",
    ),
    _p(
        r"(not made this video available in your country|not available in your country|"
        r"blocked it in your country|not available in this country|"
        r"not available in your region)",
        RestrictionType.REGION_BLOCKED,
        "RESTRICTED",
        "The uploader has not made this video available in this region.",
    ),
    _p(
        r"(copyright grounds|blocked (?:it|this video) on copyright|copyright claim|"
        r"removed (?:it )?(?:for|due to) copyright|includes content owned or licensed)",
        RestrictionType.COPYRIGHT,
        "RESTRICTED",
        "The video is blocked for copyright reasons.",
    ),
    _p(
        r"(community guidelines|terms of service|removed for violating)",
        RestrictionType.COMMUNITY_GUIDELINES,
        "RESTRICTED",
        "The video was removed for violating YouTube's Community Guidelines or Terms of Service.",
    ),
    _p(
        r"(this video is private|private video|set to private)",
        RestrictionType.PRIVATE,
        "RESTRICTED",
        "The video is private and is not visible to public viewers.",
    ),
    _p(
        r"(account associated with this video has been terminated|channel has been terminated|"
        r"video has been removed|removed by the uploader|"
        r"no longer available because)",
        RestrictionType.REMOVED,
        "RESTRICTED",
        "The video was removed (uploader deletion or terminated account).",
    ),
    _p(
        r"(members-only|join this channel to get access|channel members|"
        r"purchase to watch|rental period|this video requires payment)",
        RestrictionType.SIGN_IN_REQUIRED,
        "RESTRICTED",
        "The video is restricted to channel members or paid access.",
    ),
    _p(
        r"(sign in to watch|you need to sign in|sign in to continue|sign in to view|"
        r"this video requires sign[- ]in)",
        RestrictionType.SIGN_IN_REQUIRED,
        "RESTRICTED",
        "YouTube requires a signed-in viewer to watch this video.",
    ),
    _p(
        r"(this video (?:isn'?t|is not|is no longer|was) available|video unavailable|"
        r"this video has been removed|this short is unavailable|content is not available|"
        r"this content isn'?t available)",
        RestrictionType.UNAVAILABLE,
        "RESTRICTED",
        "YouTube reports that the video is unavailable.",
    ),
    _p(
        r"(an error occurred while (?:retrieving|playing)|playback error|"
        r"the player is having trouble|failed to load the video)",
        RestrictionType.PLAYBACK_ERROR,
        "FAILED",
        "The YouTube player reported a playback error.",
    ),
]

_CONSENT_RE = re.compile(r"before you continue to youtube", re.IGNORECASE)


def _match(text: str) -> _Pattern | None:
    if not text:
        return None
    for pattern in PATTERNS:
        if pattern.regex.search(text):
            return pattern
    return None


def _excerpt(text: str, limit: int = 300) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned[:limit]


class RestrictionDetectionService:
    """Classify a page snapshot captured by the browser monitor."""

    def classify(self, signals: dict[str, Any], http_status: int | None = None) -> Verdict:
        playability = signals.get("playability") or {}
        status = str(playability.get("status") or "").upper()
        reason = " ".join(
            part for part in (playability.get("reason"), playability.get("subreason")) if part
        )
        video = signals.get("video") or {}
        has_video = bool(video.get("present"))
        has_frames = has_video and (video.get("videoWidth") or 0) > 0
        error_texts = [str(t) for t in (signals.get("errorTexts") or []) if t]
        title = str(signals.get("title") or "")
        body = str(signals.get("bodyText") or "")

        if signals.get("hasChallenge"):
            return Verdict(
                "INCONCLUSIVE",
                RestrictionType.CAPTCHA,
                CAPTCHA_PATTERN.message,
                "explicit",
                ["automation challenge detected in page"],
            )

        # 1. Explicit player metadata.
        if reason:
            pattern = _match(reason)
            if pattern:
                return Verdict(
                    pattern.verdict,
                    pattern.restriction_type,
                    f"{pattern.message} YouTube message: \u201c{_excerpt(reason)}\u201d",
                    "explicit",
                    [_excerpt(reason)],
                )

        # 2. Player error containers rendered on the page.
        for text in error_texts:
            pattern = _match(text)
            if pattern:
                return Verdict(
                    pattern.verdict,
                    pattern.restriction_type,
                    f"{pattern.message} Page message: \u201c{_excerpt(text)}\u201d",
                    "explicit",
                    [_excerpt(text)],
                )

        # 3. Page title (e.g. "Video unavailable - YouTube").
        if title:
            pattern = _match(title)
            if pattern:
                return Verdict(
                    pattern.verdict,
                    pattern.restriction_type,
                    f"{pattern.message} Page title: \u201c{_excerpt(title)}\u201d",
                    "explicit",
                    [_excerpt(title)],
                )

        # 4. Player response status without a matching reason.
        if status in {"UNPLAYABLE", "ERROR", "LIVE_STREAM_OFFLINE", "CONTENT_CHECK_REQUIRED"}:
            if status == "ERROR":
                return Verdict(
                    "FAILED",
                    RestrictionType.PLAYBACK_ERROR,
                    "YouTube's player reported an error instead of playing the video.",
                )
            return Verdict(
                "RESTRICTED",
                RestrictionType.UNAVAILABLE,
                f"YouTube reported the video as {status.lower()}.",
            )
        if status == "LOGIN_REQUIRED":
            return Verdict(
                "RESTRICTED",
                RestrictionType.SIGN_IN_REQUIRED,
                "YouTube requires a signed-in viewer for this video "
                "(private, age-restricted or members-only).",
            )

        # 5. Body text, only when there is no playable video on the page.
        if not has_frames:
            if signals.get("consentWall") or _CONSENT_RE.search(body):
                return Verdict(
                    "INCONCLUSIVE",
                    RestrictionType.CONSENT_WALL,
                    "YouTube served a cookie consent wall instead of the player. "
                    "Availability could not be verified.",
                    "explicit",
                    ["cookie consent wall"],
                )
            pattern = _match(body)
            if pattern:
                return Verdict(
                    pattern.verdict,
                    pattern.restriction_type,
                    f"{pattern.message} Page message: \u201c{_excerpt(body, 400)}\u201d",
                    "inferred",
                    [_excerpt(body, 120)],
                )
            if signals.get("loginWall"):
                return Verdict(
                    "RESTRICTED",
                    RestrictionType.SIGN_IN_REQUIRED,
                    "YouTube asks the viewer to sign in before the video can play.",
                    "inferred",
                    ["sign-in prompt"],
                )

        if status == "OK" or has_video:
            return Verdict(
                "ACCESSIBLE",
                RestrictionType.NONE,
                "Video is publicly accessible.",
            )

        if http_status == 404:
            return Verdict(
                "RESTRICTED",
                RestrictionType.UNAVAILABLE,
                "YouTube returned HTTP 404 for this video.",
            )

        return Verdict(
            "INCONCLUSIVE",
            RestrictionType.UNKNOWN,
            "The page loaded but neither a playable video nor an explicit YouTube "
            "restriction message was found. The YouTube interface may have changed.",
            "unknown",
            ["no player, no restriction message"],
        )

    @staticmethod
    def availability_for(verdict: Verdict) -> str | None:
        if verdict.verdict == "RESTRICTED":
            return Availability.RESTRICTED
        if verdict.verdict == "FAILED":
            return Availability.FAILED
        if verdict.verdict == "INCONCLUSIVE":
            return Availability.INCONCLUSIVE
        return None
