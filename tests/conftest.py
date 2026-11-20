from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.repositories import Repository  # noqa: E402
from app.settings_store import SettingsStore  # noqa: E402


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        base_dir=tmp_path,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "monitoring.db",
        evidence_dir=tmp_path / "evidence",
        youtube_api_key=None,
        host="127.0.0.1",
        port=8000,
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_from=None,
        smtp_starttls=True,
    )


@pytest.fixture
def db(app_settings: Settings) -> Database:
    database = Database(app_settings.database_path)
    database.migrate()
    return database


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
def store(db: Database) -> SettingsStore:
    return SettingsStore(db)


@pytest.fixture
def channel(repo: Repository):
    return repo.channels.create(
        {
            "channel_url": "https://www.youtube.com/@examplechannel",
            "canonical_url": "https://www.youtube.com/@examplechannel",
            "channel_id": "UCabcdefghijklmnopqrstuv",
            "channel_name": "Example Channel",
            "handle": "examplechannel",
            "shorts_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/shorts",
            "monitoring_enabled": 1,
            "monitoring_interval_minutes": 60,
            "max_shorts_per_run": 20,
            "playback_verification_seconds": 4.0,
            "incremental_checks": 1,
            "notify_new_shorts": 1,
            "resolution_status": "resolved",
        }
    )
