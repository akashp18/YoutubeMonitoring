from __future__ import annotations

from app.domain import AlertType, NotificationStatus, RestrictionType
from app.services.notifications import NotificationService
from app.timeutil import iso
from tests.fakes import FakeHttpClient


def create_short(repo, channel):
    _new, rows = repo.shorts.upsert_discovered(
        channel["id"], [{"video_id": "abcdefghijk", "title": "Example Short"}]
    )
    return rows[0]


def create_alert(repo, channel, short, dedupe_key: str = "1:BECAME_UNAVAILABLE"):
    return repo.alerts.create_if_absent(
        {
            "channel_id": channel["id"],
            "short_id": short["id"],
            "video_id": short["video_id"],
            "alert_type": AlertType.BECAME_UNAVAILABLE,
            "previous_status": "PASS",
            "current_status": "RESTRICTED",
            "restriction_type": RestrictionType.UNAVAILABLE,
            "severity": "critical",
            "message": "Example Short is no longer accessible",
            "diagnostic_excerpt": "YouTube message: This video is unavailable",
            "dedupe_key": dedupe_key,
            "created_at": iso(),
        }
    )


def test_open_alerts_are_deduplicated(repo, channel) -> None:
    short = create_short(repo, channel)
    first = create_alert(repo, channel, short)
    second = create_alert(repo, channel, short)
    assert first is not None
    assert second is None
    assert repo.alerts.open_count() == 1

    repo.alerts.resolve(first)
    third = create_alert(repo, channel, short)
    assert third is not None
    assert repo.alerts.open_count() == 1


def test_new_short_alerts_are_one_shot(repo, channel) -> None:
    short = create_short(repo, channel)
    values = {
        "channel_id": channel["id"],
        "short_id": short["id"],
        "video_id": short["video_id"],
        "alert_type": AlertType.NEW_SHORT,
        "current_status": "NEW",
        "severity": "info",
        "message": "New Short discovered",
        "dedupe_key": f"{short['id']}:NEW_SHORT",
        "created_at": iso(),
    }
    first = repo.alerts.create_if_absent(values, suppress_if_any=True)
    repo.alerts.resolve(first)
    second = repo.alerts.create_if_absent(values, suppress_if_any=True)
    assert first is not None
    assert second is None


async def test_no_channel_configured_marks_alert_skipped(repo, channel, store, app_settings) -> None:
    short = create_short(repo, channel)
    alert_id = create_alert(repo, channel, short)
    service = NotificationService(repo, store, app_settings)
    counts = await service.process_pending()
    assert counts["skipped"] == 1
    alert = repo.alerts.get(alert_id)
    assert alert["notification_status"] == NotificationStatus.SKIPPED


async def test_webhook_delivery(repo, channel, store, app_settings) -> None:
    short = create_short(repo, channel)
    alert_id = create_alert(repo, channel, short)
    store.set_many({"notifications.webhook_url": "https://hooks.test/shorts"})
    client = FakeHttpClient()
    service = NotificationService(repo, store, app_settings, client=client)
    counts = await service.process_pending()
    assert counts["sent"] == 1
    assert len(client.posts) == 1
    payload = client.posts[0]
    assert payload["video_id"] == "abcdefghijk"
    assert payload["previous_status"] == "PASS"
    assert payload["current_status"] == "RESTRICTED"
    assert "abcdefghijk" in payload["video_url"]
    assert repo.alerts.get(alert_id)["notification_status"] == NotificationStatus.SENT


async def test_slack_format(repo, channel, store, app_settings) -> None:
    short = create_short(repo, channel)
    create_alert(repo, channel, short)
    store.set_many(
        {
            "notifications.webhook_url": "https://hooks.slack.test/x",
            "notifications.webhook_format": "slack",
        }
    )
    client = FakeHttpClient()
    service = NotificationService(repo, store, app_settings, client=client)
    await service.process_pending()
    assert list(client.posts[0].keys()) == ["text"]
    assert "Example Short" in client.posts[0]["text"]


async def test_webhook_failure_is_recorded(repo, channel, store, app_settings) -> None:
    short = create_short(repo, channel)
    alert_id = create_alert(repo, channel, short)
    store.set_many({"notifications.webhook_url": "https://hooks.test/fail"})
    client = FakeHttpClient(status=500)
    service = NotificationService(repo, store, app_settings, client=client)
    counts = await service.process_pending()
    assert counts["failed"] == 1
    alert = repo.alerts.get(alert_id)
    assert alert["notification_status"] == NotificationStatus.FAILED
    assert "HTTP 500" in (alert["notification_error"] or "")


async def test_disabled_notifications_leave_alerts_pending(repo, channel, store, app_settings) -> None:
    short = create_short(repo, channel)
    alert_id = create_alert(repo, channel, short)
    store.set_many(
        {"notifications.enabled": False, "notifications.webhook_url": "https://hooks.test/x"}
    )
    service = NotificationService(repo, store, app_settings)
    counts = await service.process_pending()
    assert counts == {"sent": 0, "failed": 0, "skipped": 0}
    assert repo.alerts.get(alert_id)["notification_status"] == NotificationStatus.PENDING


async def test_test_notification_requires_channel(repo, store, app_settings) -> None:
    service = NotificationService(repo, store, app_settings)
    ok, message = await service.send_test()
    assert ok is False
    assert "No notification channel" in message
