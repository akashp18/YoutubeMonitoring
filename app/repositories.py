from __future__ import annotations

import json
from typing import Any, Sequence

from app.db import Database
from app.domain import Availability, PlaybackStatus, RunStatus
from app.timeutil import iso

CHANNEL_FIELDS = {
    "channel_url",
    "canonical_url",
    "channel_id",
    "channel_name",
    "handle",
    "shorts_url",
    "monitoring_enabled",
    "monitoring_interval_minutes",
    "max_shorts_per_run",
    "playback_verification_seconds",
    "incremental_checks",
    "notify_new_shorts",
    "notification_overrides",
    "resolution_status",
    "resolution_error",
    "last_run_at",
    "last_success_at",
    "next_run_at",
    "consecutive_failures",
}


class ChannelsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM youtube_channels ORDER BY id ASC")

    def get(self, channel_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,))

    def find_by_youtube_id(self, youtube_channel_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM youtube_channels WHERE channel_id = ?", (youtube_channel_id,)
        )

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        payload = {key: values[key] for key in values if key in CHANNEL_FIELDS}
        payload.setdefault("channel_url", "")
        payload["created_at"] = now
        payload["updated_at"] = now
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        new_id = self.db.execute(
            f"INSERT INTO youtube_channels ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        created = self.get(new_id)
        assert created is not None
        return created

    def update(self, channel_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        payload = {key: values[key] for key in values if key in CHANNEL_FIELDS}
        if payload:
            payload["updated_at"] = iso()
            assignments = ", ".join(f"{key} = ?" for key in payload)
            self.db.execute(
                f"UPDATE youtube_channels SET {assignments} WHERE id = ?",
                (*payload.values(), channel_id),
            )
        return self.get(channel_id)

    def delete(self, channel_id: int) -> bool:
        existing = self.get(channel_id)
        if not existing:
            return False
        self.db.execute("DELETE FROM youtube_channels WHERE id = ?", (channel_id,))
        return True

    def due(self, now: str | None = None) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM youtube_channels "
            "WHERE monitoring_enabled = 1 AND monitoring_interval_minutes > 0 "
            "AND channel_id IS NOT NULL "
            "AND (next_run_at IS NULL OR next_run_at <= ?) "
            "ORDER BY COALESCE(next_run_at, created_at) ASC",
            (now or iso(),),
        )

    def scheduling_state(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT id, channel_name, channel_id, monitoring_enabled, monitoring_interval_minutes, "
            "next_run_at, last_run_at, consecutive_failures FROM youtube_channels ORDER BY id"
        )

    def set_next_run(self, channel_id: int, next_run_at: str | None) -> None:
        self.db.execute(
            "UPDATE youtube_channels SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, iso(), channel_id),
        )

    def mark_run_completed(self, channel_id: int, success: bool, error: str | None = None) -> None:
        now = iso()
        if success:
            self.db.execute(
                "UPDATE youtube_channels SET last_run_at = ?, last_success_at = ?, "
                "consecutive_failures = 0, updated_at = ? WHERE id = ?",
                (now, now, now, channel_id),
            )
        else:
            self.db.execute(
                "UPDATE youtube_channels SET last_run_at = ?, "
                "consecutive_failures = consecutive_failures + 1, updated_at = ? WHERE id = ?",
                (now, now, channel_id),
            )

    def set_resolution(self, channel_id: int, resolved: Any, error: str | None = None) -> None:
        if resolved is None:
            self.db.execute(
                "UPDATE youtube_channels SET resolution_status = 'failed', resolution_error = ?, "
                "updated_at = ? WHERE id = ?",
                (error, iso(), channel_id),
            )
            return
        handle = getattr(resolved, "handle", None)
        self.db.execute(
            "UPDATE youtube_channels SET channel_id = COALESCE(?, channel_id), "
            "channel_name = COALESCE(?, channel_name), handle = COALESCE(?, handle), "
            "canonical_url = ?, shorts_url = ?, resolution_status = 'resolved', "
            "resolution_error = NULL, updated_at = ? WHERE id = ?",
            (
                getattr(resolved, "channel_id", None),
                getattr(resolved, "channel_name", None),
                handle,
                getattr(resolved, "canonical_url", None),
                getattr(resolved, "shorts_url", None),
                iso(),
                channel_id,
            ),
        )


class ShortsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, short_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT s.*, c.channel_name, c.channel_url AS channel_canonical_url "
            "FROM youtube_shorts s LEFT JOIN youtube_channels c ON c.id = s.channel_id "
            "WHERE s.id = ?",
            (short_id,),
        )

    def get_by_video_id(self, video_id: str, channel_id: int | None = None) -> dict[str, Any] | None:
        if channel_id is not None:
            return self.db.query_one(
                "SELECT * FROM youtube_shorts WHERE video_id = ? AND channel_id = ?",
                (video_id, channel_id),
            )
        return self.db.query_one(
            "SELECT * FROM youtube_shorts WHERE video_id = ? ORDER BY id ASC LIMIT 1",
            (video_id,),
        )

    def exists(self, channel_id: int, video_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT id FROM youtube_shorts WHERE channel_id = ? AND video_id = ?",
                (channel_id, video_id),
            )
            is not None
        )

    def upsert_discovered(
        self, channel_id: int, items: Sequence[dict[str, Any]], now: str | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Insert or refresh discovered Shorts. Returns (new_rows, all_rows)."""
        timestamp = now or iso()
        if not items:
            return [], []
        video_ids = [item["video_id"] for item in items]
        existing = {
            row["video_id"]
            for row in self.db.query(
                f"SELECT video_id FROM youtube_shorts WHERE channel_id = ? AND video_id IN "
                f"({', '.join('?' for _ in video_ids)})",
                (channel_id, *video_ids),
            )
        }
        with self.db.transaction() as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO youtube_shorts (channel_id, video_id, video_url, title, "
                    "thumbnail_url, duration_seconds, first_discovered_at, last_discovered_at, "
                    "is_listed, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(channel_id, video_id) DO UPDATE SET "
                    "title = CASE WHEN excluded.title IS NOT NULL AND excluded.title <> '' "
                    "THEN excluded.title ELSE youtube_shorts.title END, "
                    "thumbnail_url = CASE WHEN excluded.thumbnail_url IS NOT NULL "
                    "AND excluded.thumbnail_url <> '' THEN excluded.thumbnail_url "
                    "ELSE youtube_shorts.thumbnail_url END, "
                    "duration_seconds = COALESCE(excluded.duration_seconds, youtube_shorts.duration_seconds), "
                    "last_discovered_at = excluded.last_discovered_at, "
                    "is_listed = 1, delisted_at = NULL, updated_at = excluded.updated_at",
                    (
                        channel_id,
                        item["video_id"],
                        item.get("video_url")
                        or f"https://www.youtube.com/shorts/{item['video_id']}",
                        item.get("title"),
                        item.get("thumbnail_url"),
                        item.get("duration_seconds"),
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
        placeholders = ", ".join("?" for _ in video_ids)
        rows = self.db.query(
            f"SELECT * FROM youtube_shorts WHERE channel_id = ? AND video_id IN ({placeholders})",
            (channel_id, *video_ids),
        )
        new_rows = [row for row in rows if row["video_id"] not in existing]
        return new_rows, rows

    def mark_delisted(
        self, channel_id: int, present_video_ids: Sequence[str], now: str | None = None
    ) -> list[dict[str, Any]]:
        timestamp = now or iso()
        if present_video_ids:
            placeholders = ", ".join("?" for _ in present_video_ids)
            rows = self.db.query(
                f"SELECT * FROM youtube_shorts WHERE channel_id = ? AND is_listed = 1 "
                f"AND video_id NOT IN ({placeholders})",
                (channel_id, *present_video_ids),
            )
            self.db.execute(
                f"UPDATE youtube_shorts SET is_listed = 0, delisted_at = ?, updated_at = ? "
                f"WHERE channel_id = ? AND is_listed = 1 AND video_id NOT IN ({placeholders})",
                (timestamp, timestamp, channel_id, *present_video_ids),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM youtube_shorts WHERE channel_id = ? AND is_listed = 1",
                (channel_id,),
            )
            self.db.execute(
                "UPDATE youtube_shorts SET is_listed = 0, delisted_at = ?, updated_at = ? "
                "WHERE channel_id = ? AND is_listed = 1",
                (timestamp, timestamp, channel_id),
            )
        return rows

    def candidates_for_check(
        self,
        channel_id: int,
        limit: int,
        pass_cutoff: str | None,
        include_pass: bool = True,
        target_short_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if target_short_id is not None:
            return self.db.query(
                "SELECT * FROM youtube_shorts WHERE id = ? AND channel_id = ?",
                (target_short_id, channel_id),
            )
        sql = (
            "SELECT * FROM youtube_shorts WHERE channel_id = ? AND is_listed = 1 AND ("
            " last_checked_at IS NULL"
            " OR last_availability_status IS NULL"
            " OR last_availability_status <> ?"
        )
        params: list[Any] = [channel_id, Availability.PASS]
        if include_pass:
            sql += " OR (last_availability_status = ? AND (? IS NULL OR last_checked_at <= ?))"
            params.extend([Availability.PASS, pass_cutoff, pass_cutoff])
        sql += (
            ") ORDER BY CASE WHEN last_checked_at IS NULL THEN 0 "
            "WHEN last_availability_status = ? THEN 2 ELSE 1 END, "
            "last_checked_at ASC, id ASC LIMIT ?"
        )
        params.extend([Availability.PASS, limit])
        return self.db.query(sql, tuple(params))

    def list(
        self,
        filter_name: str = "all",
        search: str | None = None,
        channel_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "last_checked_desc",
    ) -> dict[str, Any]:
        since = iso()
        where = ["1 = 1"]
        params: list[Any] = []
        if channel_id is not None:
            where.append("s.channel_id = ?")
            params.append(channel_id)
        if filter_name == "accessible":
            where.append("s.last_availability_status = ?")
            params.append(Availability.PASS)
        elif filter_name == "restricted":
            where.append("s.last_availability_status = ?")
            params.append(Availability.RESTRICTED)
        elif filter_name == "failed":
            where.append("s.last_availability_status = ?")
            params.append(Availability.FAILED)
        elif filter_name == "inconclusive":
            where.append("s.last_availability_status = ?")
            params.append(Availability.INCONCLUSIVE)
        elif filter_name == "warning":
            where.append("s.last_availability_status = ?")
            params.append(Availability.WARNING)
        elif filter_name == "new":
            where.append("s.first_discovered_at >= datetime('now', '-7 days')")
        elif filter_name == "changed":
            where.append("s.last_status_change_at >= datetime('now', '-7 days')")
        elif filter_name == "unchecked":
            where.append("s.last_checked_at IS NULL")
        elif filter_name == "delisted":
            where.append("s.is_listed = 0")
        if search:
            needle = f"%{search.strip()}%"
            where.append("(s.title LIKE ? OR s.video_id LIKE ?)")
            params.extend([needle, needle])
        clause = " AND ".join(where)
        order_by = {
            "last_checked_desc": "s.last_checked_at IS NULL, s.last_checked_at DESC, s.id DESC",
            "last_checked_asc": "s.last_checked_at IS NULL, s.last_checked_at ASC, s.id ASC",
            "title_asc": "s.title COLLATE NOCASE ASC",
            "discovered_desc": "s.first_discovered_at DESC, s.id DESC",
            "status": "s.last_availability_status ASC, s.id DESC",
        }.get(sort, "s.last_checked_at IS NULL, s.last_checked_at DESC, s.id DESC")
        rows = self.db.query(
            "SELECT s.*, c.channel_name, c.channel_url AS channel_canonical_url "
            "FROM youtube_shorts s LEFT JOIN youtube_channels c ON c.id = s.channel_id "
            f"WHERE {clause} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = self.db.scalar(
            "SELECT COUNT(*) FROM youtube_shorts s "
            f"LEFT JOIN youtube_channels c ON c.id = s.channel_id WHERE {clause}",
            tuple(params),
        )
        return {"items": rows, "total": int(total or 0), "limit": limit, "offset": offset}

    def update_after_check(
        self,
        short_id: int,
        *,
        availability_status: str,
        playback_status: str,
                restriction_type: str,
        playback_start_ms: int | None,
        playback_completed: bool = False,
        diagnostic_message: str | None,
        checked_at: str,
        status_changed: bool,
        failed: bool,
    ) -> None:
        self.db.execute(
            "UPDATE youtube_shorts SET last_checked_at = ?, last_availability_status = ?, "
            "last_playback_status = ?, last_restriction_type = ?, last_playback_start_ms = ?, "
            "last_playback_completed = ?, last_diagnostic_message = ?,"
            "last_status_change_at = CASE WHEN ? THEN ? ELSE last_status_change_at END, "
            "consecutive_failures = CASE WHEN ? THEN consecutive_failures + 1 ELSE 0 END, "
            "updated_at = ? WHERE id = ?",
            (
                checked_at,
                availability_status,
                playback_status,
                restriction_type,
                playback_start_ms,
                1 if playback_completed else 0,
                diagnostic_message,
                1 if status_changed else 0,
                checked_at,
                1 if failed else 0,
                checked_at,
                short_id,
            ),
        )

    def counts(self) -> dict[str, int]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN last_availability_status = 'PASS' THEN 1 ELSE 0 END) AS accessible, "
            "SUM(CASE WHEN last_availability_status = 'WARNING' THEN 1 ELSE 0 END) AS warning, "
            "SUM(CASE WHEN last_availability_status = 'RESTRICTED' THEN 1 ELSE 0 END) AS restricted, "
            "SUM(CASE WHEN last_availability_status = 'FAILED' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN last_availability_status = 'INCONCLUSIVE' THEN 1 ELSE 0 END) AS inconclusive, "
            "SUM(CASE WHEN last_checked_at IS NULL THEN 1 ELSE 0 END) AS unchecked, "
            "SUM(CASE WHEN first_discovered_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS new_shorts, "
            "SUM(CASE WHEN last_status_change_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS recently_changed "
            "FROM youtube_shorts WHERE is_listed = 1"
        ) or {}
        delisted = self.db.scalar("SELECT COUNT(*) FROM youtube_shorts WHERE is_listed = 0")
        return {
            "total": int(row.get("total") or 0),
            "accessible": int(row.get("accessible") or 0),
            "warning": int(row.get("warning") or 0),
            "restricted": int(row.get("restricted") or 0),
            "failed": int(row.get("failed") or 0),
            "inconclusive": int(row.get("inconclusive") or 0),
            "unchecked": int(row.get("unchecked") or 0),
            "new_shorts": int(row.get("new_shorts") or 0),
            "recently_changed": int(row.get("recently_changed") or 0),
            "delisted": int(delisted or 0),
        }


class RunsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        channel_id: int | None,
        trigger_type: str,
        scope: str = "channel",
        target_short_id: int | None = None,
        status: str = RunStatus.QUEUED,
    ) -> dict[str, Any]:
        now = iso()
        run_id = self.db.execute(
            "INSERT INTO monitoring_runs (channel_id, trigger_type, scope, target_short_id, "
            "status, queued_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, trigger_type, scope, target_short_id, status, now),
        )
        run = self.get(run_id)
        assert run is not None
        return run

    def get(self, run_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT r.*, c.channel_name, c.channel_id AS youtube_channel_id, "
            "s.title AS target_title, s.video_id AS target_video_id "
            "FROM monitoring_runs r "
            "LEFT JOIN youtube_channels c ON c.id = r.channel_id "
            "LEFT JOIN youtube_shorts s ON s.id = r.target_short_id "
            "WHERE r.id = ?",
            (run_id,),
        )

    def list(self, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT r.*, c.channel_name, c.channel_id AS youtube_channel_id "
            "FROM monitoring_runs r LEFT JOIN youtube_channels c ON c.id = r.channel_id "
            "ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        total = self.db.scalar("SELECT COUNT(*) FROM monitoring_runs")
        return {"items": rows, "total": int(total or 0), "limit": limit, "offset": offset}

    def latest(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT r.*, c.channel_name FROM monitoring_runs r "
            "LEFT JOIN youtube_channels c ON c.id = r.channel_id "
            "ORDER BY r.id DESC LIMIT 1"
        )

    def active(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT r.*, c.channel_name FROM monitoring_runs r "
            "LEFT JOIN youtube_channels c ON c.id = r.channel_id "
            "WHERE r.status IN ('RUNNING', 'QUEUED') ORDER BY "
            "CASE WHEN r.status = 'RUNNING' THEN 0 ELSE 1 END, r.id ASC LIMIT 1"
        )

    def running(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM monitoring_runs WHERE status = 'RUNNING' ORDER BY id ASC LIMIT 1"
        )

    def next_queued(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM monitoring_runs WHERE status = 'QUEUED' ORDER BY id ASC LIMIT 1"
        )

    def has_open_run(self, channel_id: int, scope: str = "channel") -> bool:
        row = self.db.query_one(
            "SELECT id FROM monitoring_runs WHERE channel_id = ? AND scope = ? "
            "AND status IN ('QUEUED', 'RUNNING') LIMIT 1",
            (channel_id, scope),
        )
        return row is not None

    def mark_running(self, run_id: int) -> None:
        self.db.execute(
            "UPDATE monitoring_runs SET status = 'RUNNING', started_at = ? WHERE id = ?",
            (iso(), run_id),
        )

    def request_cancel(self, run_id: int) -> bool:
        run = self.get(run_id)
        if not run:
            return False
        if run["status"] == RunStatus.QUEUED:
            self.db.execute(
                "UPDATE monitoring_runs SET status = 'CANCELLED', cancel_requested = 1, "
                "completed_at = ? WHERE id = ?",
                (iso(), run_id),
            )
            return True
        if run["status"] == RunStatus.RUNNING:
            self.db.execute(
                "UPDATE monitoring_runs SET cancel_requested = 1 WHERE id = ?", (run_id,)
            )
            return True
        return False

    def cancel_requested(self, run_id: int) -> bool:
        value = self.db.scalar(
            "SELECT cancel_requested FROM monitoring_runs WHERE id = ?", (run_id,)
        )
        return bool(value)

    def finalize(
        self, run_id: int, status: str, error_message: str | None = None
    ) -> dict[str, Any] | None:
        counts = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN availability_status = 'PASS' THEN 1 ELSE 0 END) AS passed, "
            "SUM(CASE WHEN availability_status = 'WARNING' THEN 1 ELSE 0 END) AS warning, "
            "SUM(CASE WHEN availability_status = 'RESTRICTED' THEN 1 ELSE 0 END) AS restricted, "
            "SUM(CASE WHEN availability_status = 'FAILED' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN availability_status = 'INCONCLUSIVE' THEN 1 ELSE 0 END) AS inconclusive "
            "FROM video_monitoring_results WHERE monitoring_run_id = ?",
            (run_id,),
        ) or {}
        run = self.db.query_one("SELECT started_at, queued_at FROM monitoring_runs WHERE id = ?", (run_id,))
        from app.timeutil import parse_iso

        started = parse_iso(run["started_at"] if run else None) or parse_iso(
            run["queued_at"] if run else None
        )
        duration_ms = None
        if started:
            from app.timeutil import utc_now

            duration_ms = int((utc_now() - started).total_seconds() * 1000)
        self.db.execute(
            "UPDATE monitoring_runs SET status = ?, completed_at = ?, "
            "total_checked = ?, passed_count = ?, warning_count = ?, restricted_count = ?, "
            "failed_count = ?, inconclusive_count = ?, error_message = ?, duration_ms = ? "
            "WHERE id = ?",
            (
                status,
                iso(),
                int(counts.get("total") or 0),
                int(counts.get("passed") or 0),
                int(counts.get("warning") or 0),
                int(counts.get("restricted") or 0),
                int(counts.get("failed") or 0),
                int(counts.get("inconclusive") or 0),
                error_message,
                duration_ms,
                run_id,
            ),
        )
        return self.get(run_id)

    def recover_after_restart(self) -> int:
        """Mark runs that were in flight when the app stopped as interrupted."""
        rows = self.db.query("SELECT id FROM monitoring_runs WHERE status = 'RUNNING'")
        for row in rows:
            self.db.execute(
                "UPDATE monitoring_runs SET status = 'INTERRUPTED', completed_at = ?, "
                "error_message = COALESCE(error_message, 'Interrupted by application restart; "
                "results collected before the restart were kept.') WHERE id = ?",
                (iso(), row["id"]),
            )
        return len(rows)

    def set_discovery_summary(
        self,
        run_id: int,
        *,
        source: str | None,
        total: int,
        new_count: int,
        removed_count: int,
        truncated: bool,
    ) -> None:
        self.db.execute(
            "UPDATE monitoring_runs SET discovery_source = ?, discovery_total_count = ?, "
            "discovery_new_count = ?, discovery_removed_count = ?, discovery_truncated = ? "
            "WHERE id = ?",
            (source, total, new_count, removed_count, 1 if truncated else 0, run_id),
        )

    def channel_history(self, channel_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM monitoring_runs WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        )

    def recent_failures(self, channel_id: int) -> list[str]:
        rows = self.db.query(
            "SELECT status FROM monitoring_runs WHERE channel_id = ? ORDER BY id DESC LIMIT 10",
            (channel_id,),
        )
        statuses: list[str] = []
        for row in rows:
            if row["status"] in (RunStatus.COMPLETED,):
                break
            statuses.append(row["status"])
        return statuses


class ResultsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        payload.setdefault("checked_at", iso())
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        result_id = self.db.execute(
            f"INSERT INTO video_monitoring_results ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        row = self.get(result_id)
        assert row is not None
        return row

    def get(self, result_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT r.*, s.title, s.video_url, s.channel_id FROM video_monitoring_results r "
            "LEFT JOIN youtube_shorts s ON s.id = r.short_id WHERE r.id = ?",
            (result_id,),
        )

    def latest_for_short(self, short_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM video_monitoring_results WHERE short_id = ? "
            "ORDER BY checked_at DESC, id DESC LIMIT 1",
            (short_id,),
        )

    def previous_for_short(self, short_id: int, exclude_result_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM video_monitoring_results WHERE short_id = ? AND id <> ? "
            "ORDER BY checked_at DESC, id DESC LIMIT 1",
            (short_id, exclude_result_id),
        )

    def history(self, short_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT r.*, run.trigger_type, run.channel_id AS run_channel_id, "
            "e.id AS evidence_id, e.screenshot_path "
            "FROM video_monitoring_results r "
            "LEFT JOIN monitoring_runs run ON run.id = r.monitoring_run_id "
            "LEFT JOIN monitoring_evidence e ON e.id = r.evidence_id "
            "WHERE r.short_id = ? ORDER BY r.checked_at DESC, r.id DESC LIMIT ?",
            (short_id, limit),
        )

    def for_run(self, run_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT r.*, s.title, s.thumbnail_url FROM video_monitoring_results r "
            "LEFT JOIN youtube_shorts s ON s.id = r.short_id "
            "WHERE r.monitoring_run_id = ? ORDER BY r.id ASC",
            (run_id,),
        )

    def stats_since(self, since: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS checks, "
            "AVG(CASE WHEN playback_start_time_ms IS NOT NULL THEN playback_start_time_ms END) AS avg_start "
            "FROM video_monitoring_results WHERE checked_at >= ?",
            (since,),
        ) or {}
        return {
            "checks": int(row.get("checks") or 0),
            "avg_playback_start_ms": int(row["avg_start"]) if row.get("avg_start") else None,
        }


class AlertsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_if_absent(
        self, values: dict[str, Any], suppress_if_any: bool = False
    ) -> int | None:
        """Insert an alert unless a matching dedupe key already exists.

        By default only unresolved alerts suppress a new one, so a re-opened
        issue can alert again after recovery. ``suppress_if_any`` is for
        one-shot alerts (new Short discovered, de-listed).
        """
        sql = "SELECT id FROM monitoring_alerts WHERE dedupe_key = ?"
        if not suppress_if_any:
            sql += " AND resolved_at IS NULL"
        sql += " LIMIT 1"
        existing = self.db.query_one(sql, (values["dedupe_key"],))
        if existing:
            return None
        alerts = dict(values)
        alerts.setdefault("created_at", iso())
        alerts.setdefault("severity", "warning")
        alerts.setdefault("notification_status", "PENDING")
        columns = ", ".join(alerts)
        placeholders = ", ".join("?" for _ in alerts)
        try:
            return self.db.execute(
                f"INSERT INTO monitoring_alerts ({columns}) VALUES ({placeholders})",
                tuple(alerts.values()),
            )
        except Exception:  # sqlite3.IntegrityError - raced with another insert
            return None

    def get(self, alert_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM monitoring_alerts WHERE id = ?", (alert_id,))

    def list(
        self, state: str = "open", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        where = "1 = 1"
        if state == "open":
            where = "a.resolved_at IS NULL"
        elif state == "resolved":
            where = "a.resolved_at IS NOT NULL"
        rows = self.db.query(
            "SELECT a.*, s.title, s.thumbnail_url, c.channel_name "
            "FROM monitoring_alerts a "
            "LEFT JOIN youtube_shorts s ON s.id = a.short_id "
            "LEFT JOIN youtube_channels c ON c.id = a.channel_id "
            f"WHERE {where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        total = self.db.scalar(
            f"SELECT COUNT(*) FROM monitoring_alerts a WHERE {where}"
        )
        return {"items": rows, "total": int(total or 0), "limit": limit, "offset": offset}

    def open_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM monitoring_alerts WHERE resolved_at IS NULL") or 0)

    def pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT a.*, s.title, s.video_url, c.channel_name "
            "FROM monitoring_alerts a "
            "LEFT JOIN youtube_shorts s ON s.id = a.short_id "
            "LEFT JOIN youtube_channels c ON c.id = a.channel_id "
            "WHERE a.notification_status = 'PENDING' ORDER BY a.id ASC LIMIT ?",
            (limit,),
        )

    def mark_notified(self, alert_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE monitoring_alerts SET notification_status = ?, notification_error = ?, "
            "notified_at = ? WHERE id = ?",
            (status, error, iso(), alert_id),
        )

    def resolve_for_short(self, short_id: int, alert_types: Sequence[str] | None = None) -> int:
        if alert_types:
            placeholders = ", ".join("?" for _ in alert_types)
            rows = self.db.query(
                f"SELECT id FROM monitoring_alerts WHERE short_id = ? "
                f"AND alert_type IN ({placeholders}) AND resolved_at IS NULL",
                (short_id, *alert_types),
            )
        else:
            rows = self.db.query(
                "SELECT id FROM monitoring_alerts WHERE short_id = ? AND resolved_at IS NULL",
                (short_id,),
            )
        for row in rows:
            self.db.execute(
                "UPDATE monitoring_alerts SET resolved_at = ? WHERE id = ?",
                (iso(), row["id"]),
            )
        return len(rows)

    def open_keys_for_short(self, short_id: int) -> set[str]:
        rows = self.db.query(
            "SELECT dedupe_key FROM monitoring_alerts WHERE short_id = ? AND resolved_at IS NULL",
            (short_id,),
        )
        return {row["dedupe_key"] for row in rows}

    def resolve_by_key(self, dedupe_key: str) -> int:
        rows = self.db.query(
            "SELECT id FROM monitoring_alerts WHERE dedupe_key = ? AND resolved_at IS NULL",
            (dedupe_key,),
        )
        for row in rows:
            self.db.execute(
                "UPDATE monitoring_alerts SET resolved_at = ? WHERE id = ?", (iso(), row["id"])
            )
        return len(rows)

    def resolve(self, alert_id: int) -> bool:
        if not self.get(alert_id):
            return False
        self.db.execute(
            "UPDATE monitoring_alerts SET resolved_at = ? WHERE id = ?", (iso(), alert_id)
        )
        return True

    def acknowledge(self, alert_id: int) -> bool:
        if not self.get(alert_id):
            return False
        self.db.execute(
            "UPDATE monitoring_alerts SET acknowledged_at = ? WHERE id = ?", (iso(), alert_id)
        )
        return True

    def last_for_short(self, short_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM monitoring_alerts WHERE short_id = ? ORDER BY id DESC LIMIT 1",
            (short_id,),
        )


class EvidenceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, values: dict[str, Any]) -> int:
        payload = dict(values)
        payload.setdefault("captured_at", iso())
        for key in ("console_errors", "page_errors", "failed_requests", "video_state", "page_signals"):
            if payload.get(key) is not None and not isinstance(payload[key], str):
                payload[key] = json.dumps(payload[key])
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        return self.db.execute(
            f"INSERT INTO monitoring_evidence ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )

    def get(self, evidence_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM monitoring_evidence WHERE id = ?", (evidence_id,))
        if not row:
            return None
        for key in ("console_errors", "page_errors", "failed_requests", "video_state", "page_signals"):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (ValueError, TypeError):
                    pass
        return row

    def for_result(self, result_id: int) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id FROM monitoring_evidence WHERE id = ("
            "SELECT evidence_id FROM video_monitoring_results WHERE id = ?)",
            (result_id,),
        )
        return self.get(row["id"]) if row else None

    def for_run(self, run_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT id, short_id, video_id, screenshot_path, captured_at FROM monitoring_evidence "
            "WHERE monitoring_run_id = ? ORDER BY id ASC",
            (run_id,),
        )

    def expired(self, now: str | None = None) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM monitoring_evidence WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now or iso(),),
        )

    def delete(self, evidence_id: int) -> None:
        self.db.execute("DELETE FROM monitoring_evidence WHERE id = ?", (evidence_id,))

    def screenshot_count_for_run(self, run_id: int) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM monitoring_evidence "
                "WHERE monitoring_run_id = ? AND screenshot_path IS NOT NULL",
                (run_id,),
            )
            or 0
        )


class RunLogsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, run_id: int, message: str, level: str = "info") -> None:
        self.db.execute(
            "INSERT INTO run_logs (run_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (run_id, level, message, iso()),
        )

    def list(self, run_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM run_logs WHERE run_id = ? ORDER BY id ASC LIMIT ?", (run_id, limit)
        )


class Repository:
    """Container wiring all repositories to one database."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.channels = ChannelsRepo(db)
        self.shorts = ShortsRepo(db)
        self.runs = RunsRepo(db)
        self.results = ResultsRepo(db)
        self.alerts = AlertsRepo(db)
        self.evidence = EvidenceRepo(db)
        self.run_logs = RunLogsRepo(db)
