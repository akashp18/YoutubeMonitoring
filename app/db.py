from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.config import MIGRATIONS_DIR, get_settings


class Database:
    """Small SQLite helper: a short-lived connection per operation.

    Each call opens its own connection (WAL mode, busy timeout) so the same
    object can be shared between the async web handlers, the monitoring
    scheduler and background threads without sharing a cursor.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or get_settings().database_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        conn = self.connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement and return lastrowid (or 0 when not applicable)."""
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def execute_many(self, sql: str, params: Sequence[Sequence[Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, params)

    def migrate(self, migrations_dir: Path | None = None) -> list[str]:
        """Apply numbered .sql files once each; returns applied file names."""
        directory = Path(migrations_dir or MIGRATIONS_DIR)
        conn = self.connect()
        applied: list[str] = []
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            files = sorted(p for p in directory.glob("*.sql"))
            for file in files:
                if file.name in done:
                    continue
                sql = file.read_text(encoding="utf-8")
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (file.name,),
                )
                applied.append(file.name)
        finally:
            conn.close()
        return applied

    def table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        )
        return row is not None
