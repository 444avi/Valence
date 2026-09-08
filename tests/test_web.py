"""Web layer: per-type argv whitelisting and the run lifecycle endpoints.

The subprocess is stubbed (`supervise` replaced), so these tests are fast and
offline — they exercise the API contract, the security-critical flag whitelist,
and the single-slot concurrency guard without fetching markets or calling Claude.
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

ADMIN = "avi@arboretuminvestments.net"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def access(email):
    return {"Cf-Access-Authenticated-User-Email": email}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VALENCE_HOME", str(tmp_path))
    monkeypatch.setenv("VALENCE_ADMIN_EMAIL", ADMIN)
    # Re-import config/db/app fresh so they bind to the tmp VALENCE_HOME.
    from web import config, db, jobs, app as app_mod
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(jobs)
    importlib.reload(app_mod)
    db.init_db()

    # Replace the real supervisor: mark the run done immediately with a blob,
    # so lifecycle tests never spawn a process.
    async def fake_supervise(run_id, argv):
        config.blob_path(run_id).write_text("[]")
        db.finish_run(run_id, "done", 0, str(config.blob_path(run_id)))

    monkeypatch.setattr(app_mod.jobs, "supervise", fake_supervise)
    return TestClient(app_mod.app)


# ------------------------------------------------------------- whitelist

def test_scan_rejects_max_only_flag(client):
    r = client.post("/runs", json={"type": "scan", "args": {"section": "crypto"}})
    assert r.status_code == 400
    assert "does not accept" in r.json()["detail"]


def test_max_requires_section(client):
    r = client.post("/runs", json={"type": "max", "args": {"no_llm": True}})
    assert r.status_code == 400
    assert "requires 'section'" in r.json()["detail"]


def test_bad_section_rejected(client):
    r = client.post("/runs", json={"type": "max", "args": {"section": "bogus"}})
    assert r.status_code == 400


def test_non_numeric_size_rejected(client):
    r = client.post("/runs", json={"type": "scan", "args": {"size": "; rm -rf /"}})
    assert r.status_code == 400
    assert "integer" in r.json()["detail"]


def test_unknown_type_rejected(client):
    r = client.post("/runs", json={"type": "live", "args": {}})
    assert r.status_code == 400


def test_max_min_volume_flows_to_argv(client):
    """The UI's Min-market-volume field is whitelisted for max and rendered to
    --min-volume, so a value chosen in the UI actually reaches the subprocess."""
    from web import jobs
    argv, clean = jobs.build_argv(
        "max", {"section": "crypto", "min_volume": "50000", "no_llm": True}
    )
    assert "--min-volume" in argv
    assert float(argv[argv.index("--min-volume") + 1]) == 50000.0
    assert clean["min_volume"] == "50000"


def test_scan_rejects_min_volume(client):
    # min_volume is a max-only flag; scan must not silently accept it.
    r = client.post("/runs", json={"type": "scan", "args": {"min_volume": "5000"}})
    assert r.status_code == 400
    assert "does not accept" in r.json()["detail"]


# ------------------------------------------------------------- lifecycle

def test_launch_records_attribution_and_completes(client):
    r = client.post(
        "/runs",
        json={"type": "scan", "args": {"sections": "crypto", "no_llm": True}},
        headers={"Cf-Access-Authenticated-User-Email": "bob@arboretum.net"},
    )
    assert r.status_code == 202
    run_id = r.json()["id"]

    detail = client.get(
        f"/runs/{run_id}", headers=access("bob@arboretum.net")
    ).json()
    assert detail["launched_by"] == "bob@arboretum.net"
    assert detail["status"] == "done"
    assert detail["result"] == []
    assert detail["args"] == {"sections": "crypto", "no_llm": True}


def test_concurrency_guard_blocks_second_launch(client, monkeypatch):
    from web import app as app_mod, db

    # Supervisor that leaves the run 'running' so the slot stays busy.
    async def stuck(run_id, argv):
        await asyncio.sleep(0)  # never finishes the run

    monkeypatch.setattr(app_mod.jobs, "supervise", stuck)

    first = client.post("/runs", json={"type": "scan", "args": {"no_llm": True}})
    assert first.status_code == 202
    second = client.post("/runs", json={"type": "scan", "args": {"no_llm": True}})
    assert second.status_code == 409
    assert "already" in second.json()["detail"]


def test_cancel_unknown_is_404(client):
    assert client.delete("/runs/nope").status_code == 404


def test_health_and_usage(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/usage").json()["month_to_date_llm_calls"] == 0


# ------------------------------------------------------------- multi-user access

def seed_run(run_id, email, llm_calls=0, status="done"):
    from web import config, db

    db.insert_run(run_id, "scan", "{}", email)
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


def test_regular_user_only_lists_own_runs(client):
    seed_run("alice-run", ALICE, llm_calls=3)
    seed_run("bob-run", BOB, llm_calls=7)

    response = client.get("/runs", headers=access("  ALICE@example.com "))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cf-Access-Authenticated-User-Email"
    assert [run["id"] for run in response.json()["runs"]] == ["alice-run"]

    assert client.get(
        "/runs", params={"user": BOB}, headers=access(ALICE)
    ).status_code == 403
    assert client.get(
        "/runs", params={"user": "all"}, headers=access(ALICE)
    ).status_code == 403


def test_regular_user_cannot_open_or_cancel_foreign_run(client):
    seed_run("bob-run", BOB)

    assert client.get("/runs/bob-run", headers=access(ALICE)).status_code == 404
    assert client.get(
        "/runs/bob-run/view", headers=access(ALICE)
    ).status_code == 404
    assert client.delete("/runs/bob-run", headers=access(ALICE)).status_code == 404


def test_regular_user_usage_is_private(client):
    seed_run("alice-run", ALICE, llm_calls=3)
    seed_run("bob-run", BOB, llm_calls=7)

    response = client.get("/usage", headers=access(ALICE))
    assert response.status_code == 200
    assert response.json() == {
        "month_to_date_llm_calls": 3,
        "scope": ALICE,
    }
    assert client.get(
        "/usage", params={"user": BOB}, headers=access(ALICE)
    ).status_code == 403


def test_admin_can_filter_runs_and_see_selected_plus_total_usage(client):
    seed_run("alice-run", ALICE, llm_calls=3)
    seed_run("bob-run", BOB, llm_calls=7)

    session = client.get("/session", headers=access(ADMIN)).json()
    assert session["email"] == ADMIN
    assert session["is_admin"] is True
    assert session["users"] == [ALICE, ADMIN, BOB]

    all_runs = client.get("/runs", headers=access(ADMIN)).json()["runs"]
    assert {run["id"] for run in all_runs} == {"alice-run", "bob-run"}
    alice_runs = client.get(
        "/runs", params={"user": ALICE}, headers=access(ADMIN)
    ).json()["runs"]
    assert [run["id"] for run in alice_runs] == ["alice-run"]
    assert client.get("/runs/bob-run", headers=access(ADMIN)).status_code == 200
    assert client.get("/runs/bob-run/view", headers=access(ADMIN)).status_code == 200

    alice_usage = client.get(
        "/usage", params={"user": ALICE}, headers=access(ADMIN)
    ).json()
    assert alice_usage == {
        "month_to_date_llm_calls": 3,
        "scope": ALICE,
        "total_month_to_date_llm_calls": 10,
    }
    total_usage = client.get("/usage", headers=access(ADMIN)).json()
    assert total_usage["month_to_date_llm_calls"] == 10
    assert total_usage["scope"] == "all"
    assert total_usage["total_month_to_date_llm_calls"] == 10


def test_regular_session_does_not_expose_user_directory(client):
    seed_run("bob-run", BOB)
    assert client.get("/session", headers=access(ALICE)).json() == {
        "email": ALICE,
        "is_admin": False,
    }


def test_health_does_not_expose_active_run_metadata(client):
    seed_run("active-run", BOB, status="running")
    health = client.get("/health").json()
    assert health["active_run"] is True
    assert BOB not in str(health)
    assert "active-run" not in str(health)
