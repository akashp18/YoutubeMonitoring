from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import FakeResolver


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.paused = False

    async def request_run(self, channel_id, scope="channel", target_short_id=None, trigger="manual"):
        self.calls.append((channel_id, scope, target_short_id))
        return {"id": 999, "status": "QUEUED", "channel_id": channel_id}

    async def cancel(self, run_id):
        return True

    def status(self):
        return {
            "paused": self.paused,
            "active_run": None,
            "next_due_at": None,
            "queued_runs": 0,
            "loop_running": True,
            "channels": [],
            "server_time": "2026-01-01T00:00:00Z",
        }

    def set_paused(self, paused):
        self.paused = paused
        return self.status()

    async def kick(self):
        return None

    async def run_due_now(self):
        return []


@pytest.fixture
def client(app_settings):
    app = create_app(app_settings)
    app.state.resolver = FakeResolver()
    app.state.scheduler = FakeScheduler()
    with TestClient(app) as test_client:
        yield test_client


CHANNEL_PAYLOAD = {
    "channel_url": "https://www.youtube.com/@examplechannel",
    "monitoring_interval_minutes": 60,
    "max_shorts_per_run": 25,
    "playback_verification_seconds": 5,
}


def create_channel(client) -> dict:
    response = client.post("/api/channels", json=CHANNEL_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_overview(client) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    overview = client.get("/api/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["stats"]["total"] == 0
    assert body["channels"] == 0
    assert body["interval_presets"]["15"] == "Every 15 minutes"


def test_channel_crud(client) -> None:
    created = create_channel(client)
    assert created["channel_id"] == "UCabcdefghijklmnopqrstuv"
    assert created["channel_name"] == "Example Channel"
    assert created["resolution_status"] == "resolved"
    assert created["next_run_at"] is not None

    duplicate = client.post("/api/channels", json=CHANNEL_PAYLOAD)
    assert duplicate.status_code == 409

    listing = client.get("/api/channels").json()
    assert listing["total"] == 1
    assert listing["items"][0]["shorts_count"] == 0

    patched = client.patch(f"/api/channels/{created['id']}", json={"monitoring_interval_minutes": 30})
    assert patched.status_code == 200
    assert patched.json()["monitoring_interval_minutes"] == 30

    disabled = client.patch(f"/api/channels/{created['id']}", json={"monitoring_enabled": False})
    assert disabled.json()["monitoring_enabled"] == 0
    assert disabled.json()["next_run_at"] is None

    rep = client.post("/api/channels/validate", json={"channel_url": "https://www.youtube.com/@examplechannel"})
    assert rep.json()["ok"] is True
    assert rep.json()["resolved"]["channel_id"] == "UCabcdefghijklmnopqrstuv"

    bad = client.post("/api/channels", json={"channel_url": "https://vimeo.com/@nope"})
    assert bad.status_code == 400

    bad_validate = client.post("/api/channels/validate", json={"channel_url": "not a url"})
    assert bad_validate.status_code == 200
    assert bad_validate.json()["ok"] is False

    deleted = client.delete(f"/api/channels/{created['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/channels").json()["total"] == 0


def test_shorts_listing_detail_and_recheck(client) -> None:
    created = create_channel(client)
    repo = client.app.state.repo
    repo.shorts.upsert_discovered(
        created["id"],
        [
            {"video_id": "aaaaaaaaaaa", "title": "First Short"},
            {"video_id": "bbbbbbbbbbb", "title": "Second Short"},
        ],
    )
    short = repo.shorts.get_by_video_id("aaaaaaaaaaa", created["id"])
    repo.shorts.update_after_check(
        short["id"],
        availability_status="RESTRICTED",
        playback_status="SKIPPED",
        restriction_type="UNAVAILABLE",
        playback_start_ms=None,
        diagnostic_message="This video is unavailable",
        checked_at="2026-01-01T00:00:00Z",
        status_changed=True,
        failed=True,
    )
    repo.results.insert(
        {
            "short_id": short["id"],
            "video_id": short["video_id"],
            "monitoring_run_id": None,
            "availability_status": "RESTRICTED",
            "playback_status": "SKIPPED",
            "restriction_type": "UNAVAILABLE",
            "diagnostic_message": "This video is unavailable",
            "checked_at": "2026-01-01T00:00:00Z",
        }
    )

    all_shorts = client.get("/api/shorts?filter=all").json()
    assert all_shorts["total"] == 2

    restricted = client.get("/api/shorts?filter=restricted").json()
    assert restricted["total"] == 1
    assert restricted["items"][0]["video_id"] == "aaaaaaaaaaa"

    searched = client.get("/api/shorts?q=Second").json()
    assert searched["total"] == 1

    by_channel = client.get(f"/api/shorts?channel_id={created['id']}").json()
    assert by_channel["total"] == 2

    invalid = client.get("/api/shorts?filter=bogus")
    assert invalid.status_code == 400

    detail = client.get(f"/api/shorts/{short['id']}").json()
    assert detail["short"]["video_id"] == "aaaaaaaaaaa"
    assert detail["latest"]["diagnostic_message"] == "This video is unavailable"
    assert detail["previous"] is None
    assert detail["history"]

    recheck = client.post(f"/api/shorts/{short['id']}/check")
    assert recheck.status_code == 200
    assert recheck.json()["id"] == 999
    assert client.app.state.scheduler.calls[-1][1] == "video"

    run = client.post(f"/api/channels/{created['id']}/run")
    assert run.status_code == 200
    assert run.json()["id"] == 999

    overview = client.get("/api/overview").json()
    assert overview["stats"]["restricted"] == 1


def test_settings_endpoints(client) -> None:
    settings = client.get("/api/settings").json()
    assert settings["settings"]["browser.headless"] is True
    assert "youtube_api_key_configured" in settings["environment"]

    updated = client.put(
        "/api/settings",
        json={"settings": {"browser.max_concurrent_checks": 3, "notifications.enabled": False}},
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["browser.max_concurrent_checks"] == 3
    assert updated.json()["settings"]["notifications.enabled"] is False

    unknown = client.put("/api/settings", json={"settings": {"nope.key": 1}})
    assert unknown.status_code == 400

    test = client.post("/api/settings/test-notification")
    assert test.status_code == 200
    assert test.json()["ok"] is False


def test_scheduler_endpoints(client) -> None:
    assert client.get("/api/scheduler").json()["paused"] is False
    paused = client.post("/api/scheduler/pause").json()
    assert paused["paused"] is True
    resumed = client.post("/api/scheduler/resume").json()
    assert resumed["paused"] is False
    due = client.post("/api/scheduler/run-due").json()
    assert due["count"] == 0


def test_static_dashboard_is_served(client) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "ShortsWatch" in page.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "loadOverview" in script.text
