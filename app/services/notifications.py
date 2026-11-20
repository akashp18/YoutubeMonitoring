"""NotificationService.

In-app alerts are always recorded. External delivery is optional and configured
by the user: a JSON webhook (generic / Slack / Discord) and/or SMTP e-mail.
Values are deduplicated upstream; this service only delivers pending alerts.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from app.config import Settings
from app.domain import NotificationStatus
from app.repositories import Repository
from app.settings_store import SettingsStore
from app.timeutil import iso

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(
        self,
        repo: Repository,
        settings_store: SettingsStore,
        app_settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings_store
        self.app_settings = app_settings
        self._client = client

    def channel_configured(self) -> bool:
        webhook = self.settings.get_str("notifications.webhook_url", "")
        email = self.settings.get_str("notifications.email_to", "")
        smtp_ready = bool(self.app_settings.smtp_host and self.app_settings.smtp_from)
        return bool(webhook) or bool(email and smtp_ready)

    async def process_pending(self, limit: int = 25) -> dict[str, int]:
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        if not self.settings.get_bool("notifications.enabled", True):
            return counts
        pending = self.repo.alerts.pending_notifications(limit=limit)
        for alert in pending:
            status = await self.deliver(alert)
            if status == NotificationStatus.SENT:
                counts["sent"] += 1
            elif status == NotificationStatus.FAILED:
                counts["failed"] += 1
            else:
                counts["skipped"] += 1
        return counts

    async def deliver(self, alert: dict[str, Any]) -> str:
        if not self.settings.get_bool("notifications.enabled", True):
            self.repo.alerts.mark_notified(alert["id"], NotificationStatus.SKIPPED)
            return NotificationStatus.SKIPPED

        webhook = self.settings.get_str("notifications.webhook_url", "")
        email_to = self.settings.get_str("notifications.email_to", "")
        smtp_ready = bool(self.app_settings.smtp_host and self.app_settings.smtp_from)
        if not webhook and not (email_to and smtp_ready):
            self.repo.alerts.mark_notified(
                alert["id"],
                NotificationStatus.SKIPPED,
                "No external notification channel is configured; the alert is visible in the dashboard.",
            )
            return NotificationStatus.SKIPPED

        text = self.format_text(alert)
        errors: list[str] = []
        delivered = False
        if webhook:
            ok, error = await self._post_webhook(webhook, alert, text)
            delivered = delivered or ok
            if error:
                errors.append(f"webhook: {error}")
        if email_to and smtp_ready:
            ok, error = await self._send_email(email_to, alert, text)
            delivered = delivered or ok
            if error:
                errors.append(f"email: {error}")

        if delivered:
            self.repo.alerts.mark_notified(alert["id"], NotificationStatus.SENT, "; ".join(errors) or None)
            return NotificationStatus.SENT
        self.repo.alerts.mark_notified(
            alert["id"], NotificationStatus.FAILED, "; ".join(errors)[:500] or "delivery failed"
        )
        return NotificationStatus.FAILED

    def format_text(self, alert: dict[str, Any]) -> str:
        title = alert.get("title") or alert.get("video_id") or "Unknown Short"
        url = alert.get("video_url") or (
            f"https://www.youtube.com/shorts/{alert.get('video_id')}" if alert.get("video_id") else ""
        )
        lines = [
            f"YouTube Shorts monitor: {alert.get('alert_type')}",
            f"Video: {title}",
        ]
        if url:
            lines.append(f"URL: {url}")
        if alert.get("previous_status") or alert.get("current_status"):
            lines.append(
                f"Status: {alert.get('previous_status') or 'unknown'} \u2192 "
                f"{alert.get('current_status') or 'unknown'}"
            )
        if alert.get("channel_name"):
            lines.append(f"Channel: {alert['channel_name']}")
        if alert.get("restriction_type") and alert["restriction_type"] != "NONE":
            lines.append(f"Restriction: {alert['restriction_type']}")
        if alert.get("diagnostic_excerpt"):
            lines.append(f"Diagnostic: {alert['diagnostic_excerpt']}")
        lines.append(f"Detected: {alert.get('created_at') or iso()}")
        return "\n".join(lines)

    def _payload(self, alert: dict[str, Any], text: str) -> dict[str, Any]:
        return {
            "source": "youtube-shorts-monitor",
            "alert_type": alert.get("alert_type"),
            "severity": alert.get("severity"),
            "channel": alert.get("channel_name"),
            "video_id": alert.get("video_id"),
            "title": alert.get("title"),
            "video_url": alert.get("video_url")
            or (
                f"https://www.youtube.com/shorts/{alert.get('video_id')}"
                if alert.get("video_id")
                else None
            ),
            "previous_status": alert.get("previous_status"),
            "current_status": alert.get("current_status"),
            "restriction_type": alert.get("restriction_type"),
            "diagnostic": alert.get("diagnostic_excerpt"),
            "detected_at": alert.get("created_at"),
            "text": text,
        }

    async def _post_webhook(
        self, url: str, alert: dict[str, Any], text: str
    ) -> tuple[bool, str | None]:
        fmt = self.settings.get_str("notifications.webhook_format", "generic") or "generic"
        if fmt == "slack":
            body: dict[str, Any] = {"text": text}
        elif fmt == "discord":
            body = {"content": text[:1900]}
        else:
            body = self._payload(alert, text)
        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, timeout=15)
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(url, json=body)
            if 200 <= response.status_code < 300:
                return True, None
            return False, f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            return False, str(exc)

    async def _send_email(
        self, recipients: str, alert: dict[str, Any], text: str
    ) -> tuple[bool, str | None]:
        return await asyncio.to_thread(self._send_email_sync, recipients, alert, text)

    def _send_email_sync(
        self, recipients: str, alert: dict[str, Any], text: str
    ) -> tuple[bool, str | None]:
        message = EmailMessage()
        message["Subject"] = (
            f"[Shorts monitor] {alert.get('alert_type')}: "
            f"{(alert.get('title') or alert.get('video_id') or '')[:80]}"
        )
        message["From"] = self.app_settings.smtp_from or ""
        message["To"] = recipients
        message.set_content(text)
        try:
            with smtplib.SMTP(
                self.app_settings.smtp_host or "", self.app_settings.smtp_port, timeout=20
            ) as smtp:
                if self.app_settings.smtp_starttls:
                    smtp.starttls()
                if self.app_settings.smtp_username:
                    smtp.login(
                        self.app_settings.smtp_username,
                        self.app_settings.smtp_password or "",
                    )
                smtp.send_message(message)
            return True, None
        except Exception as exc:  # noqa: BLE001 - surfacing SMTP problems is the point
            logger.warning("SMTP delivery failed: %s", exc)
            return False, str(exc)

    async def send_test(self) -> tuple[bool, str]:
        sample = {
            "id": 0,
            "alert_type": "TEST",
            "severity": "info",
            "title": "Test notification",
            "video_url": "",
            "previous_status": "PASS",
            "current_status": "PASS",
            "diagnostic_excerpt": "This is a test notification from your Shorts monitor.",
            "created_at": iso(),
        }
        if not self.channel_configured():
            return False, "No notification channel configured."
        if not self.settings.get_bool("notifications.enabled", True):
            return False, "Notifications are disabled."
        text = self.format_text(sample)
        webhook = self.settings.get_str("notifications.webhook_url", "")
        email_to = self.settings.get_str("notifications.email_to", "")
        results: list[str] = []
        ok_any = False
        if webhook:
            ok, error = await self._post_webhook(webhook, sample, text)
            ok_any = ok_any or ok
            results.append("webhook ok" if ok else f"webhook failed: {error}")
        if email_to and self.app_settings.smtp_host:
            ok, error = await self._send_email(email_to, sample, text)
            ok_any = ok_any or ok
            results.append("email ok" if ok else f"email failed: {error}")
        return ok_any, "; ".join(results)
