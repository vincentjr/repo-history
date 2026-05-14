"""SQLite storage for code-history records."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_records (
  repo                TEXT    NOT NULL,
  pr_number           INTEGER NOT NULL,
  merge_sha           TEXT,
  merged_at           TEXT,
  author              TEXT,
  record_json         TEXT    NOT NULL,
  source_updated_at   TEXT    NOT NULL,
  content_hash        TEXT    NOT NULL,
  truncated           INTEGER DEFAULT 0,
  PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS region_index (
  repo        TEXT    NOT NULL,
  file_path   TEXT    NOT NULL,
  pr_number   INTEGER NOT NULL,
  PRIMARY KEY (repo, file_path, pr_number)
);
CREATE INDEX IF NOT EXISTS idx_region_lookup ON region_index(repo, file_path);

CREATE TABLE IF NOT EXISTS sync_ledger (
  repo                         TEXT    PRIMARY KEY,
  last_successful_sync_at      TEXT,
  last_attempted_sync_at       TEXT,
  last_error                   TEXT,
  consecutive_failure_count    INTEGER DEFAULT 0,
  next_retry_at                TEXT,
  backfill_complete            INTEGER DEFAULT 0,
  backfill_cursor              INTEGER
);
"""


@dataclass
class PRRecord:
    repo: str
    pr_number: int
    merge_sha: str | None
    merged_at: str | None
    author: str | None
    record_json: dict[str, Any]
    source_updated_at: str
    content_hash: str
    truncated: bool = False


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    log.info("opening sqlite db at %s", path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    log.info("initializing schema")
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def existing_hash(conn: sqlite3.Connection, repo: str, pr_number: int) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM pr_records WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    ).fetchone()
    return row["content_hash"] if row else None


def upsert_record(
    conn: sqlite3.Connection,
    record: PRRecord,
    region_files: Iterable[str],
) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO pr_records (
                repo, pr_number, merge_sha, merged_at, author,
                record_json, source_updated_at, content_hash, truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                merge_sha = excluded.merge_sha,
                merged_at = excluded.merged_at,
                author = excluded.author,
                record_json = excluded.record_json,
                source_updated_at = excluded.source_updated_at,
                content_hash = excluded.content_hash,
                truncated = excluded.truncated
            """,
            (
                record.repo,
                record.pr_number,
                record.merge_sha,
                record.merged_at,
                record.author,
                json.dumps(record.record_json, ensure_ascii=False),
                record.source_updated_at,
                record.content_hash,
                1 if record.truncated else 0,
            ),
        )
        conn.execute(
            "DELETE FROM region_index WHERE repo = ? AND pr_number = ?",
            (record.repo, record.pr_number),
        )
        rows = [(record.repo, f, record.pr_number) for f in region_files if f]
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO region_index (repo, file_path, pr_number) VALUES (?, ?, ?)",
                rows,
            )


def records_for_file(
    conn: sqlite3.Connection,
    repo: str,
    file_path: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT p.repo, p.pr_number, p.merge_sha, p.merged_at, p.author,
               p.record_json, p.source_updated_at, p.truncated
        FROM region_index r
        JOIN pr_records p ON p.repo = r.repo AND p.pr_number = r.pr_number
        WHERE r.repo = ? AND r.file_path = ?
        ORDER BY p.merged_at DESC NULLS LAST, p.pr_number DESC
    """
    params: list[Any] = [repo, file_path]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["record"] = json.loads(d.pop("record_json"))
        d["truncated"] = bool(d["truncated"])
        out.append(d)
    return out


def touch_ledger_attempt(conn: sqlite3.Connection, repo: str, at: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO sync_ledger (repo, last_attempted_sync_at)
            VALUES (?, ?)
            ON CONFLICT(repo) DO UPDATE SET last_attempted_sync_at = excluded.last_attempted_sync_at
            """,
            (repo, at),
        )


def mark_ledger_success(conn: sqlite3.Connection, repo: str, at: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO sync_ledger (repo, last_successful_sync_at, last_attempted_sync_at, last_error, consecutive_failure_count)
            VALUES (?, ?, ?, NULL, 0)
            ON CONFLICT(repo) DO UPDATE SET
                last_successful_sync_at = excluded.last_successful_sync_at,
                last_attempted_sync_at = excluded.last_attempted_sync_at,
                last_error = NULL,
                consecutive_failure_count = 0
            """,
            (repo, at, at),
        )


def mark_ledger_error(conn: sqlite3.Connection, repo: str, at: str, error: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO sync_ledger (repo, last_attempted_sync_at, last_error, consecutive_failure_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(repo) DO UPDATE SET
                last_attempted_sync_at = excluded.last_attempted_sync_at,
                last_error = excluded.last_error,
                consecutive_failure_count = sync_ledger.consecutive_failure_count + 1
            """,
            (repo, at, error),
        )


def ledger_status(conn: sqlite3.Connection, repo: str | None = None) -> list[dict[str, Any]]:
    if repo is None:
        rows = conn.execute("SELECT * FROM sync_ledger ORDER BY repo").fetchall()
    else:
        rows = conn.execute("SELECT * FROM sync_ledger WHERE repo = ?", (repo,)).fetchall()
    return [dict(r) for r in rows]


def record_count(conn: sqlite3.Connection, repo: str | None = None) -> int:
    if repo is None:
        row = conn.execute("SELECT COUNT(*) AS c FROM pr_records").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS c FROM pr_records WHERE repo = ?", (repo,)).fetchone()
    return int(row["c"])
