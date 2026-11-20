-- Full-length playback verification.

ALTER TABLE video_monitoring_results ADD COLUMN playback_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE video_monitoring_results ADD COLUMN video_duration_seconds REAL;
ALTER TABLE youtube_shorts ADD COLUMN last_playback_completed INTEGER;
