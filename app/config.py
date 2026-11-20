from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real env vars win."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    data_dir: Path
    database_path: Path
    evidence_dir: Path
    youtube_api_key: str | None
    host: str
    port: int
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_starttls: bool


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")
    data_dir = Path(os.environ.get("YT_MON_DATA_DIR", str(BASE_DIR / "data")))
    db_path = Path(os.environ.get("YT_MON_DB_PATH", str(data_dir / "monitoring.db")))
    evidence_dir = Path(os.environ.get("YT_MON_EVIDENCE_DIR", str(BASE_DIR / "evidence")))
    data_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        base_dir=BASE_DIR,
        data_dir=data_dir,
        database_path=db_path,
        evidence_dir=evidence_dir,
        youtube_api_key=(os.environ.get("YOUTUBE_API_KEY") or "").strip() or None,
        host=os.environ.get("YT_MON_HOST", "127.0.0.1"),
        port=int(os.environ.get("YT_MON_PORT", "8000")),
        smtp_host=(os.environ.get("YT_MON_SMTP_HOST") or "").strip() or None,
        smtp_port=int(os.environ.get("YT_MON_SMTP_PORT", "587")),
        smtp_username=(os.environ.get("YT_MON_SMTP_USERNAME") or "").strip() or None,
        smtp_password=os.environ.get("YT_MON_SMTP_PASSWORD") or None,
        smtp_from=(os.environ.get("YT_MON_SMTP_FROM") or "").strip() or None,
        smtp_starttls=_bool_env("YT_MON_SMTP_STARTTLS", True),
    )
