"""Web layer: per-type argv whitelisting and the run lifecycle endpoints.

The subprocess is stubbed (`supervise` replaced), so these tests are fast and
offline — they exercise the API contract, the security-critical flag whitelist,
and the single-slot concurrency guard without fetching markets or calling Claude.
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VALENCE_HOME", str(tmp_path))
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

    detail = client.get(f"/runs/{run_id}").json()
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
