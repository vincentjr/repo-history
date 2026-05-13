from code_history import db


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_init_schema_idempotent():
    conn = make_conn()
    db.init_schema(conn)  # second call must not raise
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pr_records", "region_index", "sync_ledger"} <= tables


def test_upsert_record_and_region_lookup():
    conn = make_conn()
    rec = db.PRRecord(
        repo="acme/widgets",
        pr_number=42,
        merge_sha="abc",
        merged_at="2026-05-10T10:00:00Z",
        author="alice",
        record_json={
            "summary": "fix",
            "scope": {"files": ["src/a.py", "src/b.py"], "symbols": ["a.foo"]},
            "rationale": "because reasons",
            "rejected_alternatives": [],
            "constraints_introduced": [],
            "links": [],
        },
        source_updated_at="2026-05-10T10:00:00Z",
        content_hash="hash-1",
        truncated=False,
    )
    db.upsert_record(conn, rec, region_files=["src/a.py", "src/b.py"])

    rows = db.records_for_file(conn, "acme/widgets", "src/a.py")
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 42
    assert rows[0]["record"]["rationale"] == "because reasons"

    # No rows for an unrelated file
    assert db.records_for_file(conn, "acme/widgets", "nope.py") == []


def test_upsert_replaces_region_rows():
    conn = make_conn()
    base = db.PRRecord(
        repo="acme/widgets",
        pr_number=7,
        merge_sha=None,
        merged_at=None,
        author=None,
        record_json={"summary": "", "scope": {"files": [], "symbols": []}, "rationale": "",
                     "rejected_alternatives": [], "constraints_introduced": [], "links": []},
        source_updated_at="2026-05-10T10:00:00Z",
        content_hash="h1",
    )
    db.upsert_record(conn, base, region_files=["a.py", "b.py"])
    db.upsert_record(conn, base, region_files=["c.py"])  # changed scope
    assert db.records_for_file(conn, "acme/widgets", "a.py") == []
    assert len(db.records_for_file(conn, "acme/widgets", "c.py")) == 1


def test_existing_hash():
    conn = make_conn()
    assert db.existing_hash(conn, "acme/widgets", 1) is None
    rec = db.PRRecord(
        repo="acme/widgets",
        pr_number=1,
        merge_sha=None,
        merged_at=None,
        author=None,
        record_json={"summary": "", "scope": {"files": [], "symbols": []}, "rationale": "",
                     "rejected_alternatives": [], "constraints_introduced": [], "links": []},
        source_updated_at="t",
        content_hash="hh",
    )
    db.upsert_record(conn, rec, region_files=[])
    assert db.existing_hash(conn, "acme/widgets", 1) == "hh"


def test_ledger_transitions():
    conn = make_conn()
    db.touch_ledger_attempt(conn, "acme/widgets", "t1")
    db.mark_ledger_error(conn, "acme/widgets", "t1", "boom")
    rows = db.ledger_status(conn, "acme/widgets")
    assert rows[0]["last_error"] == "boom"
    assert rows[0]["consecutive_failure_count"] == 1

    db.mark_ledger_error(conn, "acme/widgets", "t1b", "boom2")
    rows = db.ledger_status(conn, "acme/widgets")
    assert rows[0]["consecutive_failure_count"] == 2

    db.mark_ledger_success(conn, "acme/widgets", "t2")
    rows = db.ledger_status(conn, "acme/widgets")
    assert rows[0]["last_error"] is None
    assert rows[0]["consecutive_failure_count"] == 0
    assert rows[0]["last_successful_sync_at"] == "t2"
