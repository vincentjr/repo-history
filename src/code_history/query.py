"""Region query — returns records touching a given file (and optionally a symbol)."""
from __future__ import annotations

from typing import Any

from . import db


def query_region(
    conn,
    repo: str,
    file_path: str,
    symbol: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = db.records_for_file(conn, repo, file_path, limit=None)
    if symbol:
        rows = [r for r in rows if symbol in (r["record"].get("scope") or {}).get("symbols", [])]
    if limit is not None:
        rows = rows[:limit]
    return rows
