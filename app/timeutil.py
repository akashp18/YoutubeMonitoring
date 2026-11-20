from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def minutes_ago(minutes: float) -> str:
    return iso(utc_now() - timedelta(minutes=minutes))


def add_minutes(minutes: float, base: datetime | None = None) -> str:
    return iso((base or utc_now()) + timedelta(minutes=minutes))


def format_duration_ms(ms: float | None) -> str | None:
    if ms is None:
        return None
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.1f} s"
