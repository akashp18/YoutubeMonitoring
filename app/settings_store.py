"""Typed access to the key/value settings table with sane defaults."""

from __future__ import annotations

import json
from typing import Any

from app.db import Database
from app.domain import MAX_VERIFICATION_SECONDS
from app.timeutil import iso

DEFAULTS: dict[str, Any] = {
    "browser.headless": True,
    "browser.max_concurrent_checks": 2,
    "browser.page_timeout_ms": 25000,
    "browser.playback_start_timeout_ms": 15000,
    "browser.stall_timeout_ms": 4000,
    "browser.max_retries": 1,
    "browser.locale": "en-US",
    "browser.user_agent": "",
    "browser.discovery_max_scrolls": 12,
    "browser.discovery_scroll_delay_ms": 900,
    "browser.screenshot_on_failure": True,
    "browser.mute_playback": True,
    "monitoring.max_shorts_per_run": 40,
    "monitoring.playback_verification_seconds": 5.0,
    "monitoring.verify_full_playback": False,
    "monitoring.playback_min_percent": 60,
    "monitoring.playback_max_percent": 100,
    "monitoring.continuous_cycles": True,
    "monitoring.randomize_order": True,
    "monitoring.incremental_checks": True,
    "monitoring.pass_recheck_factor": 1.0,
    "notifications.enabled": True,
    "notifications.webhook_url": "",
    "notifications.webhook_format": "generic",
    "notifications.email_to": "",
    "notifications.notify_on_new_shorts": True,
    "notifications.notify_on_recovery": True,
    "notifications.repeat_reminder_hours": 24,
    "notifications.run_failure_threshold": 3,
    "notifications.playback_regression_threshold_ms": 4000,
    "evidence.retention_days": 30,
    "evidence.max_screenshots_per_run": 25,
    "scheduler.paused": False,
}

# Bounds for values the UI may change. Secrets stay out of this table.
LIMITS: dict[str, tuple[float, float]] = {
    "browser.max_concurrent_checks": (1, 4),
    "browser.page_timeout_ms": (5000, 120000),
    "browser.playback_start_timeout_ms": (3000, 60000),
    "browser.stall_timeout_ms": (1500, 20000),
    "browser.max_retries": (0, 3),
    "browser.discovery_max_scrolls": (0, 60),
    "browser.discovery_scroll_delay_ms": (200, 5000),
    "monitoring.max_shorts_per_run": (1, 500),
    "monitoring.playback_verification_seconds": (2, MAX_VERIFICATION_SECONDS),
    "monitoring.playback_min_percent": (1, 100),
    "monitoring.playback_max_percent": (1, 100),
    "monitoring.pass_recheck_factor": (0.1, 20),
    "notifications.repeat_reminder_hours": (0, 720),
    "notifications.run_failure_threshold": (1, 50),
    "notifications.playback_regression_threshold_ms": (500, 60000),
    "evidence.retention_days": (1, 365),
    "evidence.max_screenshots_per_run": (0, 500),
}

SECRET_KEYS = {"notifications.webhook_url"}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _raw(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for row in self.db.query("SELECT key, value FROM app_settings"):
            try:
                values[row["key"]] = json.loads(row["value"])
            except (ValueError, TypeError):
                continue
        return values

    def get_all(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update(self._raw())
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        if key in DEFAULTS:
            default = DEFAULTS[key]
        row = self.db.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def get_int(self, key: str, default: int | None = None) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return int(default or 0)

    def get_float(self, key: str, default: float | None = None) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return float(default or 0.0)

    def get_bool(self, key: str, default: bool | None = None) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return "" if value is None else str(value)

    def set_many(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist known settings. Unknown keys are rejected."""
        unknown = [key for key in values if key not in DEFAULTS]
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        cleaned: dict[str, Any] = {}
        for key, value in values.items():
            if key in LIMITS:
                low, high = LIMITS[key]
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a number") from exc
                number = max(low, min(high, number))
                value = int(number) if isinstance(DEFAULTS[key], int) else number
            elif isinstance(DEFAULTS[key], bool):
                value = bool(value)
            elif key == "notifications.webhook_format":
                value = str(value or "generic").strip().lower()
                if value not in {"generic", "slack", "discord"}:
                    raise ValueError("webhook_format must be generic, slack or discord")
            elif isinstance(DEFAULTS[key], str):
                value = "" if value is None else str(value)
            cleaned[key] = value
        now = iso()
        with self.db.transaction() as conn:
            for key, value in cleaned.items():
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value), now),
                )
        return cleaned

    def public_view(self) -> dict[str, Any]:
        values = self.get_all()
        url = values.get("notifications.webhook_url") or ""
        values["notifications.webhook_url_set"] = bool(url)
        values["notifications.webhook_url"] = _mask_url(url)
        return values


def _mask_url(url: str) -> str:
    if not url:
        return ""
    if len(url) <= 12:
        return "••••"
    return f"{url[:8]}…{url[-4:]}"
