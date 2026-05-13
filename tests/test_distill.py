import pytest
from code_history.distill import build_prompt, parse_record


GOOD = """
```json
{
  "summary": "Drop legacy field X",
  "scope": {"files": ["src/foo.py"], "symbols": ["foo.bar"]},
  "rationale": "Field X was replaced by Y in v3.",
  "rejected_alternatives": ["Keep both fields"],
  "constraints_introduced": ["bar() now requires non-empty input"],
  "links": ["https://github.com/acme/widgets/issues/12"]
}
```
"""


def test_parse_fenced_json():
    r = parse_record(GOOD)
    assert r.summary.startswith("Drop")
    assert r.scope.files == ["src/foo.py"]
    assert r.constraints_introduced == ["bar() now requires non-empty input"]


def test_parse_extracts_object_from_prose():
    raw = 'Sure! Here is the record: {"summary": "x", "rationale": "y"} hope this helps.'
    r = parse_record(raw)
    assert r.summary == "x"
    assert r.rationale == "y"


def test_parse_invalid_json_raises():
    with pytest.raises(ValueError):
        parse_record("not json at all")


def test_build_prompt_contains_inputs():
    p = build_prompt("DIFF_TEXT", "DESC_TEXT", ["ISSUE_1", "ISSUE_2"])
    assert "DIFF_TEXT" in p
    assert "DESC_TEXT" in p
    assert "ISSUE_1" in p and "ISSUE_2" in p
    assert "scope" in p  # schema is embedded
