from __future__ import annotations

from datetime import timedelta

from app.domain import Availability, RestrictionType
from app.services.history import MonitoringHistoryService
from app.timeutil import iso, utc_now
from tests.fakes import pass_outcome, restricted_outcome


def add_short(repo, channel, video_id: str = "aaaaaaaaaaa", title: str = "Short A"):
    _new, rows = repo.shorts.upsert_discovered(
        channel["id"], [{"video_id": video_id, "title": title}]
    )
    return rows[0]


def test_records_results_and_updates_short_state(repo, channel, app_settings) -> None:
    short = add_short(repo, channel)
    history = MonitoringHistoryService(repo, app_settings.evidence_dir)
    first, previous = history.record(short, pass_outcome(), run_id=None)
    assert previous is None
    assert first["availability_status"] == Availability.PASS
    assert first["playback_verified"] == 1
    assert first["playback_start_time_ms"] == 400

    refreshed = repo.shorts.get(short["id"])
    assert refreshed["last_availability_status"] == Availability.PASS
    assert refreshed["last_checked_at"] is not None

    second, previous = history.record(short, restricted_outcome(), run_id=None)
    assert previous is not None
    assert previous["id"] == first["id"]
    assert repo.shorts.get(short["id"])["last_availability_status"] == Availability.RESTRICTED

    rows = repo.results.history(short["id"])
    assert len(rows) == 2
    assert rows[0]["id"] == second["id"]


def test_evidence_is_stored_with_retention(repo, channel, app_settings, tmp_path) -> None:
    short = add_short(repo, channel)
    history = MonitoringHistoryService(repo, app_settings.evidence_dir)
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG fake")
    outcome = restricted_outcome()
    outcome.screenshot_path = str(screenshot)

    result, _previous = history.record(short, outcome, run_id=None, retention_days=7)
    assert result["evidence_id"] is not None
    evidence = repo.evidence.get(result["evidence_id"])
    assert evidence is not None
    assert evidence["screenshot_path"] == str(screenshot)
    assert evidence["expires_at"] is not None
    assert evidence["page_signals"]["verdict"]["verdict"] == "RESTRICTED"


def test_expired_evidence_is_purged(repo, channel, app_settings, tmp_path) -> None:
    short = add_short(repo, channel)
    history = MonitoringHistoryService(repo, app_settings.evidence_dir)
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG fake")
    outcome = restricted_outcome()
    outcome.screenshot_path = str(screenshot)
    result, _ = history.record(short, outcome, run_id=None, retention_days=1)

    repo.db.execute(
        "UPDATE monitoring_evidence SET expires_at = ? WHERE id = ?",
        (iso(utc_now() - timedelta(days=1)), result["evidence_id"]),
    )
    removed = history.purge_expired_evidence()
    assert removed == 1
    assert repo.evidence.get(result["evidence_id"]) is None
    assert not screenshot.exists()


def test_incremental_candidates_prioritise_problems_and_skip_fresh_pass(repo, channel) -> None:
    fresh = add_short(repo, channel, "aaaaaaaaaaa", "Fresh PASS")
    stale = add_short(repo, channel, "bbbbbbbbbbb", "Stale PASS")
    failing = add_short(repo, channel, "ccccccccccc", "Failing")
    never = add_short(repo, channel, "ddddddddddd", "Never checked")

    now = utc_now()
    repo.shorts.update_after_check(
        fresh["id"],
        availability_status=Availability.PASS,
        playback_status="VERIFIED",
        restriction_type=RestrictionType.NONE,
        playback_start_ms=300,
        diagnostic_message="ok",
        checked_at=iso(now),
        status_changed=False,
        failed=False,
    )
    repo.shorts.update_after_check(
        stale["id"],
        availability_status=Availability.PASS,
        playback_status="VERIFIED",
        restriction_type=RestrictionType.NONE,
        playback_start_ms=300,
        diagnostic_message="ok",
        checked_at=iso(now - timedelta(hours=4)),
        status_changed=False,
        failed=False,
    )
    repo.shorts.update_after_check(
        failing["id"],
        availability_status=Availability.FAILED,
        playback_status="FAILED",
        restriction_type=RestrictionType.PLAYBACK_ERROR,
        playback_start_ms=None,
        diagnostic_message="stalled",
        checked_at=iso(now),
        status_changed=True,
        failed=True,
    )

    cutoff = iso(now - timedelta(hours=1))
    candidates = repo.shorts.candidates_for_check(
        channel["id"], limit=10, pass_cutoff=cutoff, include_pass=True
    )
    ids = {row["video_id"] for row in candidates}
    assert "aaaaaaaaaaa" not in ids
    assert {"bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"} <= ids
    assert candidates[0]["video_id"] == "ddddddddddd"

    problems_only = repo.shorts.candidates_for_check(
        channel["id"], limit=10, pass_cutoff=cutoff, include_pass=False
    )
    assert {row["video_id"] for row in problems_only} == {"ccccccccccc", "ddddddddddd"}
    assert never["last_checked_at"] is None


def test_discovery_preserves_history_and_marks_delisted(repo, channel) -> None:
    _new, rows = repo.shorts.upsert_discovered(
        channel["id"],
        [
            {"video_id": "aaaaaaaaaaa", "title": "A", "thumbnail_url": "https://img/a.jpg"},
            {"video_id": "bbbbbbbbbbb", "title": "B"},
        ],
    )
    assert len(_new) == 2
    first = repo.shorts.get_by_video_id("aaaaaaaaaaa", channel["id"])
    assert first["is_listed"] == 1

    _new2, rows2 = repo.shorts.upsert_discovered(
        channel["id"],
        [{"video_id": "aaaaaaaaaaa", "title": "A updated", "thumbnail_url": None}],
    )
    assert len(_new2) == 0
    assert len(rows2) == 1
    updated = repo.shorts.get_by_video_id("aaaaaaaaaaa", channel["id"])
    assert updated["title"] == "A updated"
    assert updated["thumbnail_url"] == "https://img/a.jpg"

    delisted = repo.shorts.mark_delisted(channel["id"], ["aaaaaaaaaaa"])
    assert len(delisted) == 1
    assert repo.shorts.get_by_video_id("bbbbbbbbbbb", channel["id"])["is_listed"] == 0

    repo.shorts.upsert_discovered(channel["id"], [{"video_id": "bbbbbbbbbbb", "title": "B"}])
    restored = repo.shorts.get_by_video_id("bbbbbbbbbbb", channel["id"])
    assert restored["is_listed"] == 1
    assert restored["delisted_at"] is None
