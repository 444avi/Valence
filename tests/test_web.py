"""Authenticated web lifecycle, ownership, validation, and admin behavior."""

import asyncio
import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.auth_support import VALENCE_ORIGIN, auth_for


ADMIN_ID = "acct-admin"
ALICE_ID = "acct-alice"
BOB_ID = "acct-bob"
ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("VALENCE_HOME", str(tmp_path))
    monkeypatch.setenv("VALENCE_ADMIN_ACCOUNT_ID", ADMIN_ID)
    from web import app as app_mod, config, db, jobs

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(jobs)
    importlib.reload(app_mod)
    db.init_db()

    async def fake_supervise(run_id, argv):
        config.blob_path(run_id).write_text("[]")
        db.finish_run(run_id, "done", 0, str(config.blob_path(run_id)))

    monkeypatch.setattr(app_mod.jobs, "supervise", fake_supervise)

    def client(email=ALICE, account_id=ALICE_ID, plan="max"):
        return TestClient(
            app_mod.create_app(auth=auth_for(email, account_id=account_id, plan=plan)),
            base_url=VALENCE_ORIGIN,
            follow_redirects=False,
        )

    return app_mod, db, config, client


@pytest.fixture
def client(web):
    return web[3]()


def seed_run(db, config, run_id, account_id, email, llm_calls=0, status="done"):
    db.insert_run(run_id, "scan", "{}", account_id, email)
    if status != "running":
        result_path = str(config.blob_path(run_id)) if status == "done" else None
        if result_path:
            config.blob_path(run_id).write_text("[]")
        db.finish_run(run_id, status, 0, result_path)
    conn = db.connect()
    try:
        conn.execute("UPDATE runs SET llm_calls=? WHERE id=?", (llm_calls, run_id))
        conn.commit()
    finally:
        conn.close()


def test_init_db_additively_migrates_legacy_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("VALENCE_HOME", str(tmp_path))
    from web import config, db

    importlib.reload(config)
    importlib.reload(db)
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, args TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            exit_code INTEGER, launched_by TEXT NOT NULL, result_path TEXT,
            llm_calls INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO runs (id,type,args,status,started_at,launched_by)
        VALUES ('preserved','scan','{}','done','2026-01-01T00:00:00Z','old@example.com');
        """
    )
    conn.close()

    db.init_db()
    migrated = db.get_run("preserved")
    assert migrated["account_id"] is None
    assert migrated["launched_by"] == "old@example.com"
    with db.connect() as checked:
        assert "account_id" in {
            row["name"] for row in checked.execute("PRAGMA table_info(runs)")
        }
        assert checked.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# Per-command validation remains enforced before a run is reserved.
def test_scan_rejects_max_only_flag(client):
    response = client.post("/runs", json={"type": "scan", "args": {"section": "crypto"}})
    assert response.status_code == 400
    assert "does not accept" in response.json()["detail"]


def test_max_requires_section(client):
    response = client.post("/runs", json={"type": "max", "args": {"no_llm": True}})
    assert response.status_code == 400
    assert "requires 'section'" in response.json()["detail"]


def test_bad_section_rejected(client):
    assert client.post(
        "/runs", json={"type": "max", "args": {"section": "bogus"}}
    ).status_code == 400


def test_non_numeric_size_rejected(client):
    response = client.post(
        "/runs", json={"type": "scan", "args": {"size": "; rm -rf /"}}
    )
    assert response.status_code == 400
    assert "integer" in response.json()["detail"]


def test_unknown_type_rejected(client):
    assert client.post("/runs", json={"type": "live", "args": {}}).status_code == 400


def test_max_min_volume_flows_to_argv(client):
    from web import jobs

    argv, clean = jobs.build_argv(
        "max", {"section": "crypto", "min_volume": "50000", "no_llm": True}
    )
    assert float(argv[argv.index("--min-volume") + 1]) == 50000.0
    assert clean["min_volume"] == "50000"


def test_scan_rejects_min_volume(client):
    response = client.post(
        "/runs", json={"type": "scan", "args": {"min_volume": "5000"}}
    )
    assert response.status_code == 400
    assert "does not accept" in response.json()["detail"]


def test_launch_records_stable_owner_and_completes(web, client):
    _, db, _, _ = web
    response = client.post(
        "/runs",
        json={"type": "scan", "args": {"sections": "crypto", "no_llm": True}},
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    stored = db.get_run(run_id)
    assert stored["account_id"] == ALICE_ID
    assert stored["launched_by"] == ALICE

    detail = client.get(f"/runs/{run_id}").json()
    assert "account_id" not in detail
    assert detail["status"] == "done"
    assert detail["result"] == []


def test_concurrency_guard_blocks_second_launch(web, client, monkeypatch):
    app_mod, _, _, _ = web

    async def stuck(run_id, argv):
        await asyncio.sleep(0)

    monkeypatch.setattr(app_mod.jobs, "supervise", stuck)
    body = {"type": "scan", "args": {"no_llm": True}}
    assert client.post("/runs", json=body).status_code == 202
    response = client.post("/runs", json=body)
    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def test_health_is_public_and_hides_active_metadata(web):
    _, db, config, client_for = web
    seed_run(db, config, "active-run", BOB_ID, BOB, status="running")
    health = client_for(plan="free").get("/health")
    assert health.status_code == 200
    assert health.json()["active_run"] is True
    assert BOB not in health.text
    assert "active-run" not in health.text


def test_regular_user_only_lists_and_counts_own_runs(web, client):
    _, db, config, _ = web
    seed_run(db, config, "alice-run", ALICE_ID, ALICE, llm_calls=3)
    seed_run(db, config, "bob-run", BOB_ID, BOB, llm_calls=7)

    response = client.get("/runs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert [run["id"] for run in response.json()["runs"]] == ["alice-run"]
    assert client.get("/runs", params={"user": BOB_ID}).status_code == 403
    assert client.get("/runs", params={"user": "all"}).status_code == 403
    assert client.get("/usage").json() == {
        "month_to_date_llm_calls": 3,
        "scope": ALICE_ID,
    }


def test_foreign_run_detail_view_and_cancel_are_concealed(web, client):
    _, db, config, _ = web
    seed_run(db, config, "bob-run", BOB_ID, BOB, status="running")
    assert client.get("/runs/bob-run").status_code == 404
    assert client.get("/runs/bob-run/view").status_code == 404
    assert client.delete("/runs/bob-run").status_code == 404


def test_same_account_with_changed_email_retains_ownership(web):
    _, db, config, client_for = web
    seed_run(db, config, "old-email", ALICE_ID, "old@example.com")
    renamed = client_for("new@example.com", ALICE_ID)
    assert [row["id"] for row in renamed.get("/runs").json()["runs"]] == ["old-email"]
    assert renamed.get("/runs/old-email").status_code == 200


def test_different_account_ids_with_same_email_remain_isolated(web):
    _, db, config, client_for = web
    seed_run(db, config, "first", "acct-first", "shared@example.com")
    seed_run(db, config, "second", "acct-second", "shared@example.com")
    first = client_for("shared@example.com", "acct-first")
    assert [row["id"] for row in first.get("/runs").json()["runs"]] == ["first"]
    assert first.get("/runs/second").status_code == 404


def test_legacy_email_row_is_claimed_once_and_never_reassigned(web):
    _, db, _, client_for = web
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO runs (id,type,args,status,started_at,launched_by) "
            "VALUES ('legacy','scan','{}','done','2026-01-01T00:00:00Z',?)",
            ("shared@example.com",),
        )
        conn.commit()
    finally:
        conn.close()

    first = client_for("shared@example.com", "acct-first")
    assert first.get("/runs/legacy").status_code == 200
    assert db.get_run("legacy")["account_id"] == "acct-first"

    second = client_for("shared@example.com", "acct-second")
    assert second.get("/runs/legacy").status_code == 404
    assert db.get_run("legacy")["account_id"] == "acct-first"


def test_admin_privilege_uses_stable_configured_identity(web):
    _, db, config, client_for = web
    seed_run(db, config, "alice-run", ALICE_ID, ALICE, llm_calls=3)
    seed_run(db, config, "bob-run", BOB_ID, BOB, llm_calls=7)

    # Same email and Max plan do not grant admin to the wrong account ID.
    impostor = client_for("admin@example.com", "acct-not-admin")
    assert impostor.get("/session").json()["is_admin"] is False

    admin = client_for("admin@example.com", ADMIN_ID)
    session = admin.get("/session").json()
    assert session["is_admin"] is True
    assert {row["account_id"] for row in session["users"]} == {
        ADMIN_ID,
        ALICE_ID,
        BOB_ID,
    }
    assert {row["id"] for row in admin.get("/runs").json()["runs"]} == {
        "alice-run",
        "bob-run",
    }
    assert [
        row["id"]
        for row in admin.get("/runs", params={"user": BOB_ID}).json()["runs"]
    ] == ["bob-run"]
    usage = admin.get("/usage", params={"user": ALICE_ID}).json()
    assert usage == {
        "month_to_date_llm_calls": 3,
        "scope": ALICE_ID,
        "total_month_to_date_llm_calls": 10,
    }


def test_session_is_verified_and_account_link_returns_to_valence(client):
    body = client.get("/session").json()
    assert body["email"] == ALICE
    assert body["plan"] == "max"
    assert body["is_admin"] is False
    assert body["account_url"] == (
        "https://accounts.arboretuminvestments.net/account?"
        "return_to=https%3A%2F%2Fvalence.arboretuminvestments.net%2F"
    )
    assert "users" not in body


def test_static_account_link_and_shared_auth_handler_are_present(client):
    page = client.get("/").text
    assert "Arboretum Investments" in page
    assert ">Account</a>" in page
    assert ">Log out</a>" in page
    assert "return_to=https%3A%2F%2Fvalence.arboretuminvestments.net%2F" in page
    assert "/static/auth.js?v=accounts-2" in page
    assert client.get("/static/auth.js").status_code == 200

    run_page = client.get("/static/run.html").text
    assert "Arboretum Investments" in run_page
    assert ">Account</a>" in run_page
    assert ">Log out</a>" in run_page
    assert "/static/auth.js?v=accounts-2" in run_page
