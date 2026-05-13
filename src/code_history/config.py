"""TOML config loader. Secrets may be overridden by env vars."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GENERATED_DIR = Path("generated")
DEFAULT_CONFIG_PATH = GENERATED_DIR / "config.toml"
DEFAULT_DB_PATH = GENERATED_DIR / "code-history.db"

CONFIG_SKELETON = """# code-history config

[github]
# Set token here or via env: GITHUB_TOKEN
token = ""
api_url = "https://api.github.com"
repositories = ["owner/repo"]

[llm]
provider = "cursor"  # one of: cursor, vertex, codex

[llm.cursor]
binary = "cursor-agent"
model = "claude-sonnet-4-6"
timeout_seconds = 180

[llm.vertex]
project_id = ""
location = "us-central1"
model = "gemini-2.5-pro"
timeout_seconds = 180

[llm.codex]
binary = "codex"
model = "gpt-5"
timeout_seconds = 180

[storage]
db_path = "./generated/code-history.db"

[ingest]
diff_max_bytes = 100000
"""


@dataclass
class GitHubConfig:
    token: str
    api_url: str = "https://api.github.com"
    repositories: list[str] = field(default_factory=list)


@dataclass
class CursorConfig:
    binary: str = "cursor-agent"
    model: str = "claude-sonnet-4-6"
    timeout_seconds: int = 180


@dataclass
class VertexConfig:
    project_id: str = ""
    location: str = "us-central1"
    model: str = "gemini-2.5-pro"
    timeout_seconds: int = 180


@dataclass
class CodexConfig:
    binary: str = "codex"
    model: str = "gpt-5"
    timeout_seconds: int = 180


@dataclass
class LLMConfig:
    provider: str = "cursor"
    cursor: CursorConfig = field(default_factory=CursorConfig)
    vertex: VertexConfig = field(default_factory=VertexConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)


@dataclass
class StorageConfig:
    db_path: str = "./generated/code-history.db"


@dataclass
class IngestConfig:
    diff_max_bytes: int = 100_000


@dataclass
class Config:
    github: GitHubConfig
    llm: LLMConfig
    storage: StorageConfig
    ingest: IngestConfig


class ConfigError(ValueError):
    pass


def _get(table: dict[str, Any] | None, key: str, default: Any) -> Any:
    if not table:
        return default
    return table.get(key, default)


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}. Run `code-history init` first.")
    with cfg_path.open("rb") as f:
        data = tomllib.load(f)

    gh = data.get("github") or {}
    token = os.environ.get("GITHUB_TOKEN") or gh.get("token") or ""
    if not token:
        raise ConfigError("GitHub token missing. Set [github].token or env GITHUB_TOKEN.")

    github = GitHubConfig(
        token=token,
        api_url=gh.get("api_url", "https://api.github.com"),
        repositories=list(gh.get("repositories") or []),
    )

    llm_raw = data.get("llm") or {}
    provider = llm_raw.get("provider", "cursor")
    if provider not in {"cursor", "vertex", "codex"}:
        raise ConfigError(f"Unknown llm.provider: {provider!r}")

    cursor = CursorConfig(
        binary=_get(llm_raw.get("cursor"), "binary", "cursor-agent"),
        model=_get(llm_raw.get("cursor"), "model", "claude-sonnet-4-6"),
        timeout_seconds=int(_get(llm_raw.get("cursor"), "timeout_seconds", 180)),
    )
    vertex = VertexConfig(
        project_id=_get(llm_raw.get("vertex"), "project_id", ""),
        location=_get(llm_raw.get("vertex"), "location", "us-central1"),
        model=_get(llm_raw.get("vertex"), "model", "gemini-2.5-pro"),
        timeout_seconds=int(_get(llm_raw.get("vertex"), "timeout_seconds", 180)),
    )
    codex = CodexConfig(
        binary=_get(llm_raw.get("codex"), "binary", "codex"),
        model=_get(llm_raw.get("codex"), "model", "gpt-5"),
        timeout_seconds=int(_get(llm_raw.get("codex"), "timeout_seconds", 180)),
    )
    llm = LLMConfig(provider=provider, cursor=cursor, vertex=vertex, codex=codex)

    storage = StorageConfig(
        db_path=_get(data.get("storage"), "db_path", str(DEFAULT_DB_PATH)),
    )
    ingest = IngestConfig(
        diff_max_bytes=int(_get(data.get("ingest"), "diff_max_bytes", 100_000)),
    )
    return Config(github=github, llm=llm, storage=storage, ingest=ingest)


def write_skeleton(path: str | Path | None = None) -> Path:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        raise ConfigError(f"Config already exists: {cfg_path}")
    cfg_path.write_text(CONFIG_SKELETON)
    return cfg_path
