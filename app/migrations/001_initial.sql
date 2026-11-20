-- Core schema for YouTube Shorts availability monitoring.

CREATE TABLE IF NOT EXISTS youtube_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_url TEXT NOT NULL,
    canonical_url TEXT,
    channel_id TEXT UNIQUE,
    channel_name TEXT,
    handle TEXT,
    shorts_url TEXT,
    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
    monitoring_interval_minutes INTEGER NOT NULL DEFAULT 60,
    max_shorts_per_run INTEGER NOT NULL DEFAULT 40,
    playback_verification_seconds REAL NOT NULL DEFAULT 5.0,
    incremental_checks INTEGER NOT NULL DEFAULT 1,
    notify_new_shorts INTEGER NOT NULL DEFAULT 1,
    notification_overrides TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    resolution_error TEXT,
    last_run_at TEXT,
    last_success_at TEXT,
    next_run_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channels_due
    ON youtube_channels (monitoring_enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_channels_channel_id
    ON youtube_channels (channel_id);

CREATE TABLE IF NOT EXISTS youtube_shorts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    video_url TEXT NOT NULL,
    title TEXT,
    thumbnail_url TEXT,
    duration_seconds INTEGER,
    first_discovered_at TEXT NOT NULL,
    last_discovered_at TEXT NOT NULL,
    is_listed INTEGER NOT NULL DEFAULT 1,
    delisted_at TEXT,
    last_checked_at TEXT,
    last_availability_status TEXT,
    last_playback_status TEXT,
    last_restriction_type TEXT,
    last_playback_start_ms INTEGER,
    last_status_change_at TEXT,
    last_diagnostic_message TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_shorts_video_id
    ON youtube_shorts (video_id);
CREATE INDEX IF NOT EXISTS idx_shorts_channel_listed
    ON youtube_shorts (channel_id, is_listed);
CREATE INDEX IF NOT EXISTS idx_shorts_status
    ON youtube_shorts (last_availability_status);
CREATE INDEX IF NOT EXISTS idx_shorts_checked
    ON youtube_shorts (last_checked_at);

CREATE TABLE IF NOT EXISTS monitoring_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER REFERENCES youtube_channels(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'channel',
    target_short_id INTEGER,
    status TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    discovery_source TEXT,
    discovery_new_count INTEGER NOT NULL DEFAULT 0,
    discovery_removed_count INTEGER NOT NULL DEFAULT 0,
    discovery_total_count INTEGER NOT NULL DEFAULT 0,
    discovery_truncated INTEGER NOT NULL DEFAULT 0,
    total_checked INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    restricted_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    inconclusive_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    log_summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_channel ON monitoring_runs (channel_id, queued_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON monitoring_runs (status, queued_at);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES monitoring_runs(id) ON DELETE CASCADE,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs (run_id, id);

CREATE TABLE IF NOT EXISTS video_monitoring_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id INTEGER NOT NULL REFERENCES youtube_shorts(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    monitoring_run_id INTEGER REFERENCES monitoring_runs(id) ON DELETE SET NULL,
    availability_status TEXT NOT NULL,
    playback_status TEXT NOT NULL,
    restriction_type TEXT NOT NULL DEFAULT 'NONE',
    diagnostic_message TEXT,
    playback_start_time_ms INTEGER,
    playback_verified INTEGER NOT NULL DEFAULT 0,
    playback_checked_ms INTEGER,
    page_load_ms INTEGER,
    ready_state INTEGER,
    video_width INTEGER,
    video_height INTEGER,
    checked_at TEXT NOT NULL,
    check_duration_ms INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    evidence_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_results_video
    ON video_monitoring_results (video_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_results_short
    ON video_monitoring_results (short_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_results_run
    ON video_monitoring_results (monitoring_run_id);
CREATE INDEX IF NOT EXISTS idx_results_status
    ON video_monitoring_results (availability_status, checked_at);

CREATE TABLE IF NOT EXISTS monitoring_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id INTEGER REFERENCES youtube_shorts(id) ON DELETE CASCADE,
    monitoring_run_id INTEGER REFERENCES monitoring_runs(id) ON DELETE SET NULL,
    video_id TEXT,
    screenshot_path TEXT,
    console_errors TEXT,
    page_errors TEXT,
    failed_requests TEXT,
    video_state TEXT,
    page_signals TEXT,
    browser_version TEXT,
    page_url TEXT,
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    screenshot_bytes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_evidence_run ON monitoring_evidence (monitoring_run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_expiry ON monitoring_evidence (expires_at);

CREATE TABLE IF NOT EXISTS monitoring_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER REFERENCES youtube_channels(id) ON DELETE CASCADE,
    short_id INTEGER REFERENCES youtube_shorts(id) ON DELETE CASCADE,
    video_id TEXT,
    alert_type TEXT NOT NULL,
    previous_status TEXT,
    current_status TEXT,
    restriction_type TEXT,
    severity TEXT NOT NULL DEFAULT 'warning',
    message TEXT NOT NULL,
    diagnostic_excerpt TEXT,
    dedupe_key TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'PENDING',
    notification_error TEXT,
    notified_at TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_created ON monitoring_alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_video ON monitoring_alerts (video_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_dedupe
    ON monitoring_alerts (dedupe_key) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
