"""CLI entry point: init, fetch, query, status."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH, ConfigError, load_config, write_skeleton
from .github import GitHubClient
from .ingest import fetch_repo
from .providers import build_provider
from .query import query_region


def _resolve_config(args: argparse.Namespace):
    try:
        return load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = write_skeleton(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    db_path = Path(args.config).parent / "code-history.db" if args.config else DEFAULT_DB_PATH
    conn = db.connect(db_path)
    db.init_schema(conn)
    conn.close()
    print(f"Wrote config: {path}")
    print(f"Initialized database: {db_path}")
    print("Edit the config to set [github].token and [github].repositories, then run `code-history fetch`.")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    conn = db.connect(cfg.storage.db_path)
    db.init_schema(conn)
    gh = GitHubClient(cfg.github.token, cfg.github.api_url)
    provider = build_provider(cfg)
    outcome = fetch_repo(cfg, args.repo, args.limit, conn, gh, provider)
    conn.close()
    print(json.dumps(
        {
            "repo": outcome.repo,
            "attempted": outcome.attempted,
            "distilled": outcome.distilled,
            "skipped_unchanged": outcome.skipped_unchanged,
            "errors": [{"pr": n, "error": m} for n, m in outcome.errors],
        },
        indent=2,
    ))
    return 0 if not outcome.errors or outcome.distilled > 0 else 1


def cmd_query(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    conn = db.connect(cfg.storage.db_path)
    db.init_schema(conn)
    rows = query_region(conn, args.repo, args.file, symbol=args.symbol, limit=args.limit)
    conn.close()
    print(json.dumps(rows, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    conn = db.connect(cfg.storage.db_path)
    db.init_schema(conn)
    ledger = db.ledger_status(conn, args.repo)
    counts = {}
    if args.repo:
        counts[args.repo] = db.record_count(conn, args.repo)
    else:
        for r in cfg.github.repositories:
            counts[r] = db.record_count(conn, r)
    conn.close()
    print(json.dumps({"ledger": ledger, "record_counts": counts}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="code-history")
    p.add_argument("--config", help="Path to config TOML (default: ~/.config/code-history/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="Create config skeleton and initialize the database")
    pi.set_defaults(func=cmd_init)

    pf = sub.add_parser("fetch", help="Fetch the latest N PRs from a repo and distill them")
    pf.add_argument("--repo", required=True, help="owner/repo")
    pf.add_argument("--limit", type=int, default=25)
    pf.set_defaults(func=cmd_fetch)

    pq = sub.add_parser("query", help="Return records touching a file")
    pq.add_argument("--repo", required=True)
    pq.add_argument("--file", required=True, help="repo-relative file path")
    pq.add_argument("--symbol", default=None)
    pq.add_argument("--limit", type=int, default=None)
    pq.set_defaults(func=cmd_query)

    ps = sub.add_parser("status", help="Show ingest state and record counts")
    ps.add_argument("--repo", default=None)
    ps.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
