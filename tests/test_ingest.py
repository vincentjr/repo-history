import json
from dataclasses import dataclass
from typing import Any

from code_history import db
from code_history.config import (
    Config, GitHubConfig, IngestConfig, LLMConfig, StorageConfig,
)
from code_history.distill import Record, Scope
from code_history.github import PullRequest
from code_history.ingest import fetch_repo
from code_history.providers.base import Provider


class FakeGitHub:
    def __init__(self, prs, diffs, comments=None, issues=None):
        self._prs = prs
        self._diffs = diffs
        self._comments = comments or {}
        self._issues = issues or {}

    def list_recent_merged_prs(self, repo, limit):
        return self._prs[:limit]

    def get_pr_diff(self, repo, number):
        return self._diffs[number]

    def get_review_comments(self, repo, number):
        return self._comments.get(number, [])

    def get_linked_issues(self, repo, body):
        return self._issues.get(body, [])


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, record):
        self._record = record
        self.calls = 0

    def _generate(self, prompt: str) -> str:
        self.calls += 1
        return self._record.model_dump_json()


def _cfg():
    return Config(
        github=GitHubConfig(token="t"),
        llm=LLMConfig(provider="cursor"),
        storage=StorageConfig(db_path=":memory:"),
        ingest=IngestConfig(diff_max_bytes=10_000),
    )


def _pr(n, body="body"):
    return PullRequest(
        number=n, title=f"PR {n}", body=body,
        user_login="alice", merge_commit_sha=f"sha{n}",
        merged_at="2026-05-10T10:00:00Z",
        updated_at="2026-05-10T10:00:00Z",
        html_url=f"https://example/{n}",
    )


def test_fetch_distills_and_persists_regions():
    cfg = _cfg()
    conn = db.connect(":memory:")
    db.init_schema(conn)
    prs = [_pr(1), _pr(2)]
    diffs = {1: "diff --git a/x b/x\n", 2: "diff --git a/y b/y\n"}
    rec = Record(
        summary="s", scope=Scope(files=["src/x.py"], symbols=["x.foo"]),
        rationale="r",
    )
    provider = FakeProvider(rec)
    gh = FakeGitHub(prs, diffs)

    outcome = fetch_repo(cfg, "acme/widgets", limit=10, conn=conn, gh=gh, provider=provider)
    assert outcome.attempted == 2
    assert outcome.distilled == 2
    assert outcome.skipped_unchanged == 0

    rows = db.records_for_file(conn, "acme/widgets", "src/x.py")
    assert len(rows) == 2
    assert {r["pr_number"] for r in rows} == {1, 2}


def test_fetch_skips_unchanged_on_second_run():
    cfg = _cfg()
    conn = db.connect(":memory:")
    db.init_schema(conn)
    prs = [_pr(1)]
    diffs = {1: "same"}
    rec = Record(summary="s", scope=Scope(files=["a.py"]), rationale="r")
    provider = FakeProvider(rec)
    gh = FakeGitHub(prs, diffs)

    first = fetch_repo(cfg, "acme/widgets", 10, conn, gh, provider)
    assert first.distilled == 1
    assert provider.calls == 1

    second = fetch_repo(cfg, "acme/widgets", 10, conn, gh, provider)
    assert second.distilled == 0
    assert second.skipped_unchanged == 1
    assert provider.calls == 1  # not called again


def test_fetch_truncates_oversize_diff():
    cfg = _cfg()
    cfg.ingest.diff_max_bytes = 200
    conn = db.connect(":memory:")
    db.init_schema(conn)
    big = "diff --git a/big b/big\n" + ("x" * 10_000)
    prs = [_pr(1)]
    gh = FakeGitHub(prs, {1: big})
    rec = Record(summary="s", scope=Scope(files=["big"]), rationale="r")
    provider = FakeProvider(rec)

    fetch_repo(cfg, "acme/widgets", 10, conn, gh, provider)
    row = conn.execute("SELECT truncated FROM pr_records WHERE pr_number=1").fetchone()
    assert row["truncated"] == 1
