"""GitHub REST client. Handles rate limiting and linked-issue resolution."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests


log = logging.getLogger(__name__)


_LINKED_ISSUE_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)
_CROSS_REPO_REF_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+([\w.-]+/[\w.-]+)#(\d+)",
    re.IGNORECASE,
)


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    user_login: str | None
    merge_commit_sha: str | None
    merged_at: str | None
    updated_at: str
    html_url: str
    raw: dict[str, Any] = field(default_factory=dict)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com", session: requests.Session | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "code-history/0.1",
            }
        )

    # --- low-level ---

    def _request(self, method: str, url: str, **kw: Any) -> requests.Response:
        full = url if url.startswith("http") else f"{self.api_url}{url}"
        log.info("GitHub %s %s", method, full)
        while True:
            resp = self._session.request(method, full, **kw)
            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(1, reset - int(time.time())) + 1
                log.warning("rate-limited; sleeping %ds until reset", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise GitHubError(f"Unauthorized (401): {resp.text[:200]}")
            if resp.status_code >= 400:
                raise GitHubError(f"GitHub {method} {full} -> {resp.status_code}: {resp.text[:200]}")
            return resp

    def _paginate(self, url: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        next_url: str | None = url
        first = True
        while next_url:
            resp = self._request("GET", next_url, params=params if first else None)
            first = False
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    yield item
            else:
                yield data
                return
            next_url = _next_link(resp.headers.get("Link"))

    # --- high-level ---

    def list_recent_merged_prs(self, repo: str, limit: int) -> list[PullRequest]:
        """Newest-first by updated time. Filters out unmerged PRs."""
        out: list[PullRequest] = []
        params = {"state": "closed", "sort": "updated", "direction": "desc"}
        for item in self._paginate(f"/repos/{repo}/pulls", params=params):
            if not item.get("merged_at"):
                continue
            out.append(_to_pr(item))
            if len(out) >= limit:
                break
        return out

    def get_pr_diff(self, repo: str, number: int) -> str:
        resp = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    def get_review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for c in self._paginate(f"/repos/{repo}/issues/{number}/comments"):
            comments.append(c)
        for c in self._paginate(f"/repos/{repo}/pulls/{number}/comments"):
            comments.append(c)
        return comments

    def get_linked_issues(self, repo: str, body: str) -> list[str]:
        """Resolve closing keywords in a PR body to issue bodies. Cross-repo refs are best-effort."""
        out: list[str] = []
        seen: set[tuple[str, int]] = set()
        for match in _LINKED_ISSUE_RE.finditer(body or ""):
            num = int(match.group(1))
            key = (repo, num)
            if key in seen:
                continue
            seen.add(key)
            text = self._fetch_issue_body(repo, num)
            if text:
                out.append(text)
        for match in _CROSS_REPO_REF_RE.finditer(body or ""):
            other_repo = match.group(1)
            num = int(match.group(2))
            key = (other_repo, num)
            if key in seen:
                continue
            seen.add(key)
            text = self._fetch_issue_body(other_repo, num)
            if text:
                out.append(text)
        return out

    def _fetch_issue_body(self, repo: str, number: int) -> str | None:
        try:
            resp = self._request("GET", f"/repos/{repo}/issues/{number}")
        except GitHubError:
            return None
        data = resp.json()
        title = data.get("title") or ""
        body = data.get("body") or ""
        return f"[{repo}#{number}] {title}\n\n{body}".strip()


def _to_pr(item: dict[str, Any]) -> PullRequest:
    user = item.get("user") or {}
    return PullRequest(
        number=int(item["number"]),
        title=item.get("title") or "",
        body=item.get("body") or "",
        user_login=user.get("login"),
        merge_commit_sha=item.get("merge_commit_sha"),
        merged_at=item.get("merged_at"),
        updated_at=item.get("updated_at") or "",
        html_url=item.get("html_url") or "",
        raw=item,
    )


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if section.endswith('rel="next"'):
            start = section.find("<")
            end = section.find(">")
            if start != -1 and end != -1:
                return section[start + 1 : end]
    return None
