# code-history

A local CLI that distills GitHub pull requests into structured rationale records (SQLite-backed) and answers code-region queries. The goal: at the moment a coding agent or developer modifies a file, surface the *why* behind how that file got to be the way it is — trade-offs accepted, alternatives rejected, constraints introduced — instead of inferring intent from current code alone.

See `specs/code-history-context-system.md` for the full design.

## Status

MVP. The following work end-to-end:

- `init` — write a config skeleton and initialize the database
- `fetch` — pull the latest N merged PRs from a repo and distill each into a record
- `query` — return records that touch a given file (optionally filtered by symbol)
- `status` — report sync-ledger state and record counts

Deferred (the data model accommodates these without migration): `sync`, `backfill`, retry/backoff scheduling.

## Install

Requires Python 3.11+. One command sets up the virtual environment and installs dependencies:

```bash
./setup.sh
```

This creates `.venv/` and installs the package in editable mode. Re-run any time to refresh dependencies.

For the Vertex AI provider, also install the optional extra:

```bash
.venv/bin/pip install -e ".[vertex]"
```

After setup, drive everything through the `./code-history` wrapper — it calls the venv's Python for you, so you never need to touch `.venv/` directly.

## Where things live

The `.venv/` directory is only for the Python environment and is managed by `./setup.sh`. Everything the tool generates — config, database — lives under `./generated/` in the project root (gitignored). Override with `--config /path/to/config.toml` if you want it elsewhere.

## Configure

```bash
./code-history init
```

This creates `./generated/config.toml` and `./generated/code-history.db`. Edit the config:

```toml
[github]
token = ""                              # or export GITHUB_TOKEN=...
api_url = "https://api.github.com"      # override for GitHub Enterprise
repositories = ["owner/repo"]

[llm]
provider = "cursor"                     # default; alternatives: "vertex", "codex"

[llm.cursor]
binary = "cursor-agent"                 # must be on PATH
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
diff_max_bytes = 100000                 # diffs above this are truncated
```

Pass `--config /path/to/config.toml` on any command to use a different location.

### Secrets

- `GITHUB_TOKEN` env var takes precedence over `[github].token`.
- For Vertex, auth via Application Default Credentials (`gcloud auth application-default login`) or `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service-account JSON.

### Provider prerequisites

| Provider | What it needs |
|---|---|
| `cursor` (default) | `cursor-agent` binary on PATH (or set `[llm.cursor].binary`) |
| `codex` | `codex` binary on PATH |
| `vertex` | `pip install -e ".[vertex]"`, ADC or service-account, and `project_id` set |

## Use

### Fetch and distill

```bash
./code-history fetch --repo owner/repo --limit 25 -v
```

Walks the latest 25 merged PRs newest-first. For each: pulls diff + body + review comments + linked issues, hashes the inputs, skips if unchanged, truncates oversize diffs, calls the configured LLM provider, and upserts the resulting record. Idempotent — re-running won't re-distill unchanged PRs.

Output is a JSON summary:

```json
{
  "repo": "owner/repo",
  "attempted": 25,
  "distilled": 23,
  "skipped_unchanged": 2,
  "errors": []
}
```

### Query for a file's history

```bash
./code-history query --repo owner/repo --file src/auth/session.py
```

Returns all records whose `scope.files` includes the path, newest-merged first:

```json
[
  {
    "repo": "owner/repo",
    "pr_number": 1421,
    "merge_sha": "...",
    "merged_at": "2026-04-30T...",
    "author": "alice",
    "source_updated_at": "...",
    "truncated": false,
    "record": {
      "summary": "...",
      "scope": {"files": [...], "symbols": [...]},
      "rationale": "...",
      "rejected_alternatives": [...],
      "constraints_introduced": [...],
      "links": [...]
    }
  }
]
```

Optional flags:

- `--symbol foo.bar` — filter to records that list this symbol in `scope.symbols`
- `--limit 10` — cap the number of results

### Status

```bash
./code-history status                  # all repos in config
./code-history status --repo owner/repo
```

Returns ledger state (last attempted/successful sync, last error) and record counts.

## How it fits into a coding workflow

`query` is meant to be called by another agent or tool before it edits a file. The returned JSON is intended to be fed to that agent so it can decide which records bear on the proposed change. This tool does *not* judge relevance itself.

## Failure modes (handled)

- **GitHub rate limit exhausted** — pauses until reset, resumes from the current PR.
- **Diff too large** — truncated to file list + partial content; `truncated = 1` on the record.
- **Linked issue inaccessible or cross-repo** — skipped, continues with available content.
- **PR amended after merge** — `source_updated_at` changes → `content_hash` changes → re-distilled.
- **Token revoked / scope insufficient** — fails fast with a clear error and writes it to `sync_ledger.last_error`.
- **Provider invalid JSON or timeout** — retries up to 3 times per PR; on persistent failure, writes an error placeholder record and continues the batch.

## Development

```bash
.venv/bin/python -m pytest
```

(Tests are the one spot the venv is invoked directly, since pytest isn't part of the `code-history` CLI.)

Layout:

```
src/code_history/
├── cli.py              # argparse dispatcher
├── config.py           # TOML loader + env override
├── db.py               # SQLite schema, upserts, ledger
├── github.py           # REST client w/ rate-limit pause + linked-issue resolution
├── distill.py          # Pydantic Record schema + shared prompt + fence-tolerant parser
├── providers/
│   ├── base.py         # Provider ABC + 3-attempt retry wrapper
│   ├── cursor.py       # shell-out
│   ├── codex.py        # shell-out
│   ├── vertex.py       # google-cloud-aiplatform
│   └── factory.py
├── ingest.py           # fetch loop: hash → skip → truncate → distill → upsert
└── query.py
```

## Before relying on it

Per the spec's validation gate: hand-write the ideal record for 20–30 diverse PRs from one subsystem, iterate the prompt (per provider — Cursor, Codex, and Vertex will produce subtly different records) until generated records match the hand-written ones in substance, then confirm `query` surfaces the rationale a senior engineer would have flagged for 10 real coding tasks. Only after that should the system be scaled to additional repos.
