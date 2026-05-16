from code_history import db


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_init_schema_idempotent():
    conn = make_conn()
    db.init_schema(conn)  # second call must not raise
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "repos",
        "pull_requests",
        "source_artifacts",
        "decisions",
        "code_refs",
        "constraints",
        "rejected_alternatives",
        "evidence_links",
        "sync_ledger",
    } <= tables
    assert "pr_records" not in tables
    assert "region_index" not in tables


def test_upsert_record_and_region_lookup():
    conn = make_conn()
    rec = db.PRRecord(
        repo="acme/widgets",
        pr_number=42,
        merge_sha="abc",
        merged_at="2026-05-10T10:00:00Z",
        author="alice",
        record={
            "summary": "fix",
            "scope": {"files": ["src/a.py", "src/b.py"], "symbols": ["a.foo"]},
            "rationale": "because reasons",
            "rejected_alternatives": ["keep old behavior"],
            "constraints_introduced": ["callers expect stable ordering"],
            "links": ["https://github.com/acme/widgets/issues/42"],
        },
        source_updated_at="2026-05-10T10:00:00Z",
        content_hash="hash-1",
        truncated=False,
        title="Fix widget ordering",
        html_url="https://github.com/acme/widgets/pull/42",
        provider="cursor",
        model="claude-sonnet-4-6",
    )
    db.upsert_record(
        conn,
        rec,
        region_files=["src/a.py", "src/b.py"],
        source_artifacts=[db.SourceArtifact(kind="diff", body="diff --git a/src/a.py b/src/a.py\n")],
    )

    rows = db.records_for_file(conn, "acme/widgets", "src/a.py")
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 42
    assert rows[0]["record"]["rationale"] == "because reasons"
    assert rows[0]["record"]["constraints_introduced"] == ["callers expect stable ordering"]
    assert rows[0]["record"]["rejected_alternatives"] == ["keep old behavior"]
    assert rows[0]["record"]["links"] == ["https://github.com/acme/widgets/issues/42"]

    repo = conn.execute("SELECT full_name, owner, name FROM repos").fetchone()
    assert dict(repo) == {"full_name": "acme/widgets", "owner": "acme", "name": "widgets"}

    pr = conn.execute(
        """
        SELECT p.title, p.html_url, p.content_hash
        FROM pull_requests p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.full_name = 'acme/widgets' AND p.number = 42
        """
    ).fetchone()
    assert pr["title"] == "Fix widget ordering"
    assert pr["html_url"] == "https://github.com/acme/widgets/pull/42"
    assert pr["content_hash"] == "hash-1"

    decision = conn.execute("SELECT summary, rationale, provider, model FROM decisions").fetchone()
    assert dict(decision) == {
        "summary": "fix",
        "rationale": "because reasons",
        "provider": "cursor",
        "model": "claude-sonnet-4-6",
    }
    assert conn.execute("SELECT COUNT(*) AS c FROM code_refs").fetchone()["c"] == 2
    assert conn.execute("SELECT text FROM constraints").fetchone()["text"] == "callers expect stable ordering"
    assert conn.execute("SELECT text FROM rejected_alternatives").fetchone()["text"] == "keep old behavior"
    assert conn.execute("SELECT url FROM evidence_links").fetchone()["url"] == "https://github.com/acme/widgets/issues/42"
    assert conn.execute("SELECT kind FROM source_artifacts").fetchone()["kind"] == "diff"

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
        record={"summary": "", "scope": {"files": [], "symbols": []}, "rationale": "",
                "rejected_alternatives": [], "constraints_introduced": [], "links": []},
        source_updated_at="2026-05-10T10:00:00Z",
        content_hash="h1",
    )
    db.upsert_record(conn, base, region_files=["a.py", "b.py"])
    db.upsert_record(conn, base, region_files=["c.py"])  # changed scope
    assert db.records_for_file(conn, "acme/widgets", "a.py") == []
    assert len(db.records_for_file(conn, "acme/widgets", "c.py")) == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM code_refs").fetchone()["c"] == 1


def test_existing_hash():
    conn = make_conn()
    assert db.existing_hash(conn, "acme/widgets", 1) is None
    rec = db.PRRecord(
        repo="acme/widgets",
        pr_number=1,
        merge_sha=None,
        merged_at=None,
        author=None,
        record={"summary": "", "scope": {"files": [], "symbols": []}, "rationale": "",
                "rejected_alternatives": [], "constraints_introduced": [], "links": []},
        source_updated_at="t",
        content_hash="hh",
    )
    db.upsert_record(conn, rec, region_files=[])
    assert db.existing_hash(conn, "acme/widgets", 1) == "hh"


def test_ledger_transitions():
    conn = make_conn()
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sync_ledger)")}
    assert columns == {
        "repo",
        "last_successful_sync_at",
        "last_attempted_sync_at",
        "last_error",
        "consecutive_failure_count",
    }

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
