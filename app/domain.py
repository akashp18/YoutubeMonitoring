"""Domain vocabulary shared by services, storage and the API."""

from __future__ import annotations


class Availability:
    """Overall outcome of a monitoring check."""

    PASS = "PASS"
    WARNING = "WARNING"
    RESTRICTED = "RESTRICTED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"

    ALL = (PASS, WARNING, RESTRICTED, FAILED, INCONCLUSIVE)
    PROBLEM = (WARNING, RESTRICTED, FAILED, INCONCLUSIVE)


class PlaybackStatus:
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    ALL = (VERIFIED, NOT_VERIFIED, FAILED, SKIPPED)


class RestrictionType:
    """Why a video is not normally playable."""

    NONE = "NONE"
    UNAVAILABLE = "UNAVAILABLE"
    REMOVED = "REMOVED"
    PRIVATE = "PRIVATE"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    REGION_BLOCKED = "REGION_BLOCKED"
    COPYRIGHT = "COPYRIGHT"
    COMMUNITY_GUIDELINES = "COMMUNITY_GUIDELINES"
    SIGN_IN_REQUIRED = "SIGN_IN_REQUIRED"
    CAPTCHA = "CAPTCHA"
    CONSENT_WALL = "CONSENT_WALL"
    PLAYBACK_ERROR = "PLAYBACK_ERROR"
    UNKNOWN = "UNKNOWN"

    ALL = (
        NONE,
        UNAVAILABLE,
        REMOVED,
        PRIVATE,
        AGE_RESTRICTED,
        REGION_BLOCKED,
        COPYRIGHT,
        COMMUNITY_GUIDELINES,
        SIGN_IN_REQUIRED,
        CAPTCHA,
        CONSENT_WALL,
        PLAYBACK_ERROR,
        UNKNOWN,
    )


class RunStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"

    ACTIVE = (QUEUED, RUNNING)
    FINISHED = (COMPLETED, FAILED, CANCELLED, INTERRUPTED)


class TriggerType:
    SCHEDULE = "schedule"
    MANUAL = "manual"
    RECHECK = "recheck"


class AlertType:
    NEW_SHORT = "NEW_SHORT"
    BECAME_UNAVAILABLE = "BECAME_UNAVAILABLE"
    BECAME_RESTRICTED = "BECAME_RESTRICTED"
    PLAYBACK_FAILING = "PLAYBACK_FAILING"
    PLAYBACK_PERFORMANCE = "PLAYBACK_PERFORMANCE"
    RECOVERED = "RECOVERED"
    DELISTED = "DELISTED"
    RUN_FAILURES = "RUN_FAILURES"


class NotificationStatus:
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"
    SKIPPED = "SKIPPED"


class DiscoverySource:
    API = "youtube_api"
    BROWSER = "browser"
    HTML = "html"


INTERVAL_PRESETS: dict[int, str] = {
    15: "Every 15 minutes",
    30: "Every 30 minutes",
    60: "Every hour",
    360: "Every 6 hours",
    720: "Every 12 hours",
    1440: "Daily",
    0: "Manual only",
}

# Seconds of real playback we are willing to generate per check. Capped so the
# monitor cannot be used to accumulate watch time.
MAX_VERIFICATION_SECONDS = 15.0
