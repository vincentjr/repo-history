"""Fetch flow: GitHub PR -> distilled Record -> SQLite."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from . import db
from .config import Config
from .distill import PROMPT_VERSION
from .github import GitHubClient, PullRequest
from .providers import Provider, ProviderError


log = logging.getLogger(__name__)


@dataclass
class FetchOutcome:
    repo: str
    attempted: int
    distilled: int
    skipped_unchanged: int
    errors: list[tuple[int, str]]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _hash_inputs(diff: str, description: str, linked_issues: Iterable[str], review_comments: Iterable[str]) -> str:
    h = hashlib.sha256()
    h.update(b"diff\0")
    h.update(diff.encode("utf-8", errors="replace"))
    h.update(b"\0desc\0")
    h.update(description.encode("utf-8", errors="replace"))
    h.update(b"\0issues\0")
    for s in linked_issues:
        h.update(s.encode("utf-8", errors="replace"))
        h.update(b"\0")
    h.update(b"comments\0")
    for s in review_comments:
        h.update(s.encode("utf-8", errors="replace"))
        h.update(b"\0")
    return h.hexdigest()


def _truncate_diff(diff: str, max_bytes: int) -> tuple[str, bool]:
    encoded = diff.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return diff, False
    head_lines: list[str] = []
    file_paths: list[str] = []
    running = 0
    cap = max_bytes // 2
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            file_paths.append(line)
        running += len(line) + 1
        head_lines.append(line)
        if running >= cap:
            break
    file_list = "\n".join(file_paths) if file_paths else "(could not detect file list)"
    truncated = (
        "[diff truncated due to size]\n"
        f"Files in this PR:\n{file_list}\n\n"
        "--- begin partial diff ---\n"
        + "\n".join(head_lines)
    )
    return truncated, True


def _review_text(comments: list[dict]) -> list[str]:
    out: list[str] = []
    for c in comments:
        author = (c.get("user") or {}).get("login") or "?"
        body = c.get("body") or ""
        if body.strip():
            out.append(f"[{author}] {body.strip()}")
    return out


def _compose_description(pr: PullRequest, review_comments: list[str]) -> str:
    parts = [f"Title: {pr.title}", "", pr.body or "(no body)"]
    if review_comments:
        parts.append("")
        parts.append("--- Review comments ---")
        parts.extend(review_comments)
    return "\n".join(parts)


def _provider_model(cfg: Config) -> str | None:
    provider_cfg = getattr(cfg.llm, cfg.llm.provider, None)
    return getattr(provider_cfg, "model", None)


def _source_artifacts(
    pr: PullRequest,
    diff: str,
    comments: list[dict],
    linked_issues: list[str],
) -> list[db.SourceArtifact]:
    artifacts = [
        db.SourceArtifact(kind="diff", body=diff, url=pr.html_url),
        db.SourceArtifact(
            kind="pr_body",
            body=f"Title: {pr.title}\n\n{pr.body or ''}",
            author=pr.user_login,
            url=pr.html_url,
        ),
    ]
    for comment in comments:
        body = (comment.get("body") or "").strip()
        if not body:
            continue
        user = comment.get("user") or {}
        artifacts.append(
            db.SourceArtifact(
                kind="review_comment",
                body=body,
                author=user.get("login"),
                url=comment.get("html_url"),
            )
        )
    for issue in linked_issues:
        if issue.strip():
            artifacts.append(db.SourceArtifact(kind="linked_issue", body=issue))
    return artifacts


def fetch_repo(
    cfg: Config,
    repo: str,
    limit: int,
    conn,
    gh: GitHubClient,
    provider: Provider,
) -> FetchOutcome:
    started = now_iso()
    db.touch_ledger_attempt(conn, repo, started)
    log.info("fetch start repo=%s limit=%d", repo, limit)
    prs = gh.list_recent_merged_prs(repo, limit)
    outcome = FetchOutcome(repo=repo, attempted=len(prs), distilled=0, skipped_unchanged=0, errors=[])

    for pr in prs:
        try:
            diff = gh.get_pr_diff(repo, pr.number)
            comments = gh.get_review_comments(repo, pr.number)
            review_text = _review_text(comments)
            linked = gh.get_linked_issues(repo, pr.body or "")

            content_hash = _hash_inputs(diff, pr.body or "", linked, review_text)
            prior = db.existing_hash(conn, repo, pr.number)
            if prior == content_hash:
                outcome.skipped_unchanged += 1
                log.info("skip pr=%d (unchanged)", pr.number)
                continue

            diff_for_llm, truncated = _truncate_diff(diff, cfg.ingest.diff_max_bytes)
            description = _compose_description(pr, review_text)

            record = provider.distill(diff_for_llm, description, linked)

            db.upsert_record(
                conn,
                db.PRRecord(
                    repo=repo,
                    pr_number=pr.number,
                    merge_sha=pr.merge_commit_sha,
                    merged_at=pr.merged_at,
                    author=pr.user_login,
                    record=record.model_dump(),
                    source_updated_at=pr.updated_at,
                    content_hash=content_hash,
                    truncated=truncated,
                    title=pr.title,
                    html_url=pr.html_url,
                    api_url=cfg.github.api_url,
                    provider=provider.name,
                    model=_provider_model(cfg),
                    prompt_version=PROMPT_VERSION,
                ),
                region_files=record.scope.files,
                source_artifacts=_source_artifacts(pr, diff, comments, linked),
            )
            outcome.distilled += 1
            log.info("distilled pr=%d files=%d", pr.number, len(record.scope.files))
        except ProviderError as e:
            outcome.errors.append((pr.number, str(e)))
            log.warning("provider error pr=%d: %s", pr.number, e)
            _write_error_placeholder(conn, repo, pr, str(e))
        except Exception as e:
            outcome.errors.append((pr.number, f"{type(e).__name__}: {e}"))
            log.exception("unexpected error pr=%d", pr.number)

    finished = now_iso()
    if outcome.errors and outcome.distilled == 0:
        db.mark_ledger_error(conn, repo, finished, "; ".join(f"#{n}: {m[:80]}" for n, m in outcome.errors[:3]))
    else:
        db.mark_ledger_success(conn, repo, finished)
    return outcome


def _write_error_placeholder(conn, repo: str, pr: PullRequest, message: str) -> None:
    """Record that a PR was attempted but distillation failed, so we don't endlessly retry within the same batch."""
    record = {
        "summary": "",
        "scope": {"files": [], "symbols": []},
        "rationale": "",
        "rejected_alternatives": [],
        "constraints_introduced": [],
        "links": [],
        "_error": message,
    }
    db.upsert_record(
        conn,
        db.PRRecord(
            repo=repo,
            pr_number=pr.number,
            merge_sha=pr.merge_commit_sha,
            merged_at=pr.merged_at,
            author=pr.user_login,
            record=record,
            source_updated_at=pr.updated_at,
            content_hash=f"error:{hashlib.sha256(message.encode()).hexdigest()[:16]}",
            truncated=False,
        ),
        region_files=[],
    )
