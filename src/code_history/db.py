"""SQLite storage for code-history records."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name       TEXT    NOT NULL,
  owner           TEXT,
  name            TEXT,
  api_url         TEXT    NOT NULL DEFAULT 'https://api.github.com',
  default_branch  TEXT,
  created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(full_name, api_url)
);

CREATE TABLE IF NOT EXISTS pull_requests (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id            INTEGER NOT NULL,
  number             INTEGER NOT NULL,
  title              TEXT,
  author             TEXT,
  merged_at          TEXT,
  merge_sha          TEXT,
  source_updated_at  TEXT    NOT NULL,
  html_url           TEXT,
  content_hash       TEXT    NOT NULL,
  truncated          INTEGER DEFAULT 0,
  created_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
  updated_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (repo_id) REFERENCES repos(id) ON DELETE CASCADE,
  UNIQUE(repo_id, number)
);
CREATE INDEX IF NOT EXISTS idx_pull_requests_repo_number ON pull_requests(repo_id, number);
CREATE INDEX IF NOT EXISTS idx_pull_requests_merged_at ON pull_requests(repo_id, merged_at);

CREATE TABLE IF NOT EXISTS source_artifacts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id         INTEGER NOT NULL,
  kind          TEXT    NOT NULL,
  ordinal       INTEGER NOT NULL DEFAULT 0,
  author        TEXT,
  url           TEXT,
  body          TEXT    NOT NULL,
  content_hash  TEXT    NOT NULL,
  created_at    TEXT    DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_pr ON source_artifacts(pr_id);

CREATE TABLE IF NOT EXISTS decisions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id           INTEGER NOT NULL,
  decision_index  INTEGER NOT NULL DEFAULT 0,
  summary         TEXT    NOT NULL DEFAULT '',
  rationale       TEXT    NOT NULL DEFAULT '',
  confidence      REAL,
  record_version  INTEGER NOT NULL DEFAULT 1,
  prompt_version  TEXT    NOT NULL DEFAULT '1',
  provider        TEXT,
  model           TEXT,
  extra_json      TEXT    NOT NULL DEFAULT '{}',
  created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
  UNIQUE(pr_id, decision_index)
);
CREATE INDEX IF NOT EXISTS idx_decisions_pr ON decisions(pr_id);

CREATE TABLE IF NOT EXISTS code_refs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id   INTEGER NOT NULL,
  file_path     TEXT    NOT NULL,
  old_file_path TEXT,
  symbol        TEXT,
  language      TEXT,
  start_line    INTEGER,
  end_line      INTEGER,
  change_type   TEXT,
  FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_code_refs_lookup ON code_refs(file_path, symbol);
CREATE INDEX IF NOT EXISTS idx_code_refs_decision ON code_refs(decision_id);

CREATE TABLE IF NOT EXISTS constraints (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id  INTEGER NOT NULL,
  text         TEXT    NOT NULL,
  FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_constraints_decision ON constraints(decision_id);

CREATE TABLE IF NOT EXISTS rejected_alternatives (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id  INTEGER NOT NULL,
  text         TEXT    NOT NULL,
  FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rejected_alternatives_decision ON rejected_alternatives(decision_id);

CREATE TABLE IF NOT EXISTS evidence_links (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id          INTEGER NOT NULL,
  source_artifact_id   INTEGER,
  url                 TEXT,
  quote_or_span        TEXT,
  relevance            TEXT,
  FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
  FOREIGN KEY (source_artifact_id) REFERENCES source_artifacts(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_decision ON evidence_links(decision_id);

CREATE TABLE IF NOT EXISTS sync_ledger (
  repo                         TEXT    PRIMARY KEY,
  last_successful_sync_at      TEXT,
  last_attempted_sync_at       TEXT,
  last_error                   TEXT,
  consecutive_failure_count    INTEGER DEFAULT 0
);
"""


@dataclass
class PRRecord:
    repo: str
    pr_number: int
    merge_sha: str | None
    merged_at: str | None
    author: str | None
    record: dict[str, Any]
    source_updated_at: str
    content_hash: str
    truncated: bool = False
    title: str | None = None
    html_url: str | None = None
    api_url: str = "https://api.github.com"
    provider: str | None = None
    model: str | None = None
    record_version: int = 1
    prompt_version: str = "1"


@dataclass
class SourceArtifact:
    kind: str
    body: str
    author: str | None = None
    url: str | None = None


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
        """
        SELECT p.content_hash
        FROM pull_requests p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.full_name = ? AND p.number = ?
        """,
        (repo, pr_number),
    ).fetchone()
    return row["content_hash"] if row else None


def upsert_record(
    conn: sqlite3.Connection,
    record: PRRecord,
    region_files: Iterable[str],
    source_artifacts: Iterable[SourceArtifact] = (),
) -> None:
    with transaction(conn):
        files = _dedupe(region_files)
        _upsert_normalized_record(conn, record, files, list(source_artifacts))


def records_for_file(
    conn: sqlite3.Connection,
    repo: str,
    file_path: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return _normalized_records_for_file(conn, repo, file_path, limit)


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
        row = conn.execute("SELECT COUNT(*) AS c FROM pull_requests").fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM pull_requests p
            JOIN repos r ON r.id = p.repo_id
            WHERE r.full_name = ?
            """,
            (repo,),
        ).fetchone()
    return int(row["c"])


def _upsert_normalized_record(
    conn: sqlite3.Connection,
    record: PRRecord,
    region_files: list[str],
    source_artifacts: list[SourceArtifact],
) -> None:
    repo_id = _ensure_repo(conn, record.repo, record.api_url)
    pr_id = _upsert_pull_request(conn, repo_id, record)

    conn.execute("DELETE FROM source_artifacts WHERE pr_id = ?", (pr_id,))
    for i, artifact in enumerate(source_artifacts):
        if not artifact.body:
            continue
        conn.execute(
            """
            INSERT INTO source_artifacts (pr_id, kind, ordinal, author, url, body, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pr_id,
                artifact.kind,
                i,
                artifact.author,
                artifact.url,
                artifact.body,
                _artifact_hash(artifact.kind, artifact.body),
            ),
        )

    conn.execute("DELETE FROM decisions WHERE pr_id = ?", (pr_id,))
    cursor = conn.execute(
        """
        INSERT INTO decisions (
            pr_id, decision_index, summary, rationale, record_version, prompt_version,
            provider, model, extra_json
        ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pr_id,
            str(record.record.get("summary") or ""),
            str(record.record.get("rationale") or ""),
            record.record_version,
            record.prompt_version,
            record.provider,
            record.model,
            json.dumps(_extra_record_fields(record.record), ensure_ascii=False),
        ),
    )
    decision_id = int(cursor.lastrowid)

    scope = record.record.get("scope") or {}
    files = region_files or _dedupe(scope.get("files") or [])
    symbols = _dedupe(scope.get("symbols") or [])
    code_ref_rows = _code_ref_rows(files, symbols)
    if code_ref_rows:
        conn.executemany(
            "INSERT INTO code_refs (decision_id, file_path, symbol) VALUES (?, ?, ?)",
            [(decision_id, file_path, symbol) for file_path, symbol in code_ref_rows],
        )

    _insert_text_rows(conn, "constraints", decision_id, record.record.get("constraints_introduced") or [])
    _insert_text_rows(conn, "rejected_alternatives", decision_id, record.record.get("rejected_alternatives") or [])

    links = _dedupe(record.record.get("links") or [])
    if links:
        conn.executemany(
            "INSERT INTO evidence_links (decision_id, url) VALUES (?, ?)",
            [(decision_id, link) for link in links if link],
        )


def _normalized_records_for_file(
    conn: sqlite3.Connection,
    repo: str,
    file_path: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT DISTINCT d.id AS decision_id, r.full_name AS repo, p.number AS pr_number,
               p.merge_sha, p.merged_at, p.author, p.source_updated_at, p.truncated,
               d.summary, d.rationale, d.extra_json
        FROM code_refs c
        JOIN decisions d ON d.id = c.decision_id
        JOIN pull_requests p ON p.id = d.pr_id
        JOIN repos r ON r.id = p.repo_id
        WHERE r.full_name = ? AND c.file_path = ?
        ORDER BY p.merged_at DESC NULLS LAST, p.number DESC, d.decision_index ASC
    """
    params: list[Any] = [repo, file_path]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_record_from_decision_row(conn, row) for row in rows]


def _record_from_decision_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    decision_id = int(row["decision_id"])
    files = [
        r["file_path"]
        for r in conn.execute(
            "SELECT DISTINCT file_path FROM code_refs WHERE decision_id = ? AND file_path != '' ORDER BY file_path",
            (decision_id,),
        )
    ]
    symbols = [
        r["symbol"]
        for r in conn.execute(
            "SELECT DISTINCT symbol FROM code_refs WHERE decision_id = ? AND symbol IS NOT NULL ORDER BY symbol",
            (decision_id,),
        )
    ]
    constraints_ = _text_values(conn, "constraints", decision_id)
    alternatives = _text_values(conn, "rejected_alternatives", decision_id)
    links = [
        r["url"]
        for r in conn.execute(
            "SELECT url FROM evidence_links WHERE decision_id = ? AND url IS NOT NULL ORDER BY id",
            (decision_id,),
        )
    ]
    record = {
        "summary": row["summary"],
        "scope": {"files": files, "symbols": symbols},
        "rationale": row["rationale"],
        "rejected_alternatives": alternatives,
        "constraints_introduced": constraints_,
        "links": links,
    }
    record.update(json.loads(row["extra_json"] or "{}"))
    return {
        "repo": row["repo"],
        "pr_number": row["pr_number"],
        "merge_sha": row["merge_sha"],
        "merged_at": row["merged_at"],
        "author": row["author"],
        "source_updated_at": row["source_updated_at"],
        "truncated": bool(row["truncated"]),
        "record": record,
    }


def _ensure_repo(conn: sqlite3.Connection, full_name: str, api_url: str) -> int:
    owner, name = _split_repo(full_name)
    conn.execute(
        """
        INSERT INTO repos (full_name, owner, name, api_url, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(full_name, api_url) DO UPDATE SET
            owner = excluded.owner,
            name = excluded.name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (full_name, owner, name, api_url),
    )
    row = conn.execute(
        "SELECT id FROM repos WHERE full_name = ? AND api_url = ?",
        (full_name, api_url),
    ).fetchone()
    return int(row["id"])


def _upsert_pull_request(conn: sqlite3.Connection, repo_id: int, record: PRRecord) -> int:
    conn.execute(
        """
        INSERT INTO pull_requests (
            repo_id, number, title, author, merged_at, merge_sha,
            source_updated_at, html_url, content_hash, truncated, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(repo_id, number) DO UPDATE SET
            title = excluded.title,
            author = excluded.author,
            merged_at = excluded.merged_at,
            merge_sha = excluded.merge_sha,
            source_updated_at = excluded.source_updated_at,
            html_url = excluded.html_url,
            content_hash = excluded.content_hash,
            truncated = excluded.truncated,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            repo_id,
            record.pr_number,
            record.title,
            record.author,
            record.merged_at,
            record.merge_sha,
            record.source_updated_at,
            record.html_url,
            record.content_hash,
            1 if record.truncated else 0,
        ),
    )
    row = conn.execute(
        "SELECT id FROM pull_requests WHERE repo_id = ? AND number = ?",
        (repo_id, record.pr_number),
    ).fetchone()
    return int(row["id"])


def _insert_text_rows(conn: sqlite3.Connection, table: str, decision_id: int, values: Iterable[Any]) -> None:
    rows = [(decision_id, str(value)) for value in values if value]
    if rows:
        conn.executemany(f"INSERT INTO {table} (decision_id, text) VALUES (?, ?)", rows)


def _text_values(conn: sqlite3.Connection, table: str, decision_id: int) -> list[str]:
    return [
        r["text"]
        for r in conn.execute(f"SELECT text FROM {table} WHERE decision_id = ? ORDER BY id", (decision_id,))
    ]


def _code_ref_rows(files: list[str], symbols: list[str]) -> list[tuple[str, str | None]]:
    if not symbols:
        return [(file_path, None) for file_path in files]
    return [(file_path, symbol) for file_path in files for symbol in symbols]


def _extra_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    known = {
        "summary",
        "scope",
        "rationale",
        "rejected_alternatives",
        "constraints_introduced",
        "links",
    }
    return {key: value for key, value in record.items() if key not in known}


def _artifact_hash(kind: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(kind.encode("utf-8", errors="replace"))
    h.update(b"\0")
    h.update(body.encode("utf-8", errors="replace"))
    return h.hexdigest()


def _split_repo(full_name: str) -> tuple[str | None, str | None]:
    if "/" not in full_name:
        return None, full_name
    owner, name = full_name.split("/", 1)
    return owner or None, name or None


def _dedupe(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
