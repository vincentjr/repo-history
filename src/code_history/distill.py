"""Record schema and shared distillation prompt."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class Scope(BaseModel):
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class Record(BaseModel):
    summary: str = ""
    scope: Scope = Field(default_factory=Scope)
    rationale: str = ""
    rejected_alternatives: list[str] = Field(default_factory=list)
    constraints_introduced: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


PROMPT_VERSION = "1"


SCHEMA_DOC = """{
  "summary": "one or two sentences on what the PR does",
  "scope": {
    "files": ["path/to/file.py", "..."],
    "symbols": ["module.function_name", "Class.method", "..."]
  },
  "rationale": "WHY the change was made — what problem, motivation, or constraint drove it",
  "rejected_alternatives": [
    "approach the author considered and rejected, with the reason"
  ],
  "constraints_introduced": [
    "an invariant, dependency, or trade-off this PR locks in that future changes must respect"
  ],
  "links": ["github issue url", "design doc url", "..."]
}"""


PROMPT_TEMPLATE = """You are distilling a merged GitHub pull request into a structured rationale record.
Future coding agents will read this record to understand *why* the code is the way it is.

Output ONLY a single JSON object matching this schema (no prose, no code fences):
{schema}

Rules:
- Base every field on evidence in the diff, description, or linked issues. Do not invent.
- If a field has no evidence, return an empty string or empty list.
- `summary` describes what changed; `rationale` is the *why*.
- `rejected_alternatives` are alternatives the PR author explicitly considered and rejected (in the description or review comments). Do not speculate.
- `constraints_introduced` are things a future change must not violate (e.g., "this function must remain pure", "callers rely on FIFO ordering"). Only include if clearly implied by the diff or commentary.
- `scope.files` should be the paths actually modified in the diff.
- `scope.symbols` should be functions/classes/methods materially changed (best-effort; empty if unclear).
- Keep prose tight. No marketing language.

---
PR DESCRIPTION:
{description}

---
LINKED ISSUES:
{linked_issues}

---
DIFF:
{diff}
"""


def build_prompt(diff: str, description: str, linked_issues: list[str]) -> str:
    issues_block = "\n\n".join(linked_issues) if linked_issues else "(none)"
    return PROMPT_TEMPLATE.format(
        schema=SCHEMA_DOC,
        description=description or "(empty)",
        linked_issues=issues_block,
        diff=diff or "(empty)",
    )


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_record(raw: str) -> Record:
    """Strip optional fence markers, locate the JSON object, and validate against Record."""
    text = _FENCE_RE.sub("", raw).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model output: {raw[:200]!r}")
    blob = text[start : end + 1]
    try:
        data: Any = json.loads(blob)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from model: {e}") from e
    try:
        return Record.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Record validation failed: {e}") from e
