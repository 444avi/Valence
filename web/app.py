"""Valence web layer: a job runner over the arb CLI, plus a static front end.

Single FastAPI process, serving JSON and the UI from the same origin (no CORS).
Every web-launched job is run-to-completion (`scan` or `max`). The run view
polls GET /runs/{id} until status flips to done/failed — there is no streaming
(plan §3).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, jobs


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Valence", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _prevent_private_response_caching(request: Request, call_next):
    """Keep Cloudflare/browser caches from mixing identity-scoped responses."""
    response = await call_next(request)
    if (
        request.url.path == "/session"
        or request.url.path == "/usage"
        or request.url.path.startswith("/runs")
    ):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cf-Access-Authenticated-User-Email"
    return response


def _current_user(request: Request) -> str:
    """Attribution from Cloudflare Access. Trustworthy only because the origin is
    unreachable except through the tunnel (plan §10) — not validated here."""
    email = request.headers.get(config.ACCESS_EMAIL_HEADER, "").strip().lower()
    return email or config.UNKNOWN_USER


def _is_admin(user: str) -> bool:
    return user == config.ADMIN_EMAIL


def _can_access_run(row: dict[str, Any], user: str) -> bool:
    return _is_admin(user) or str(row["launched_by"]).strip().lower() == user


def _authorized_run(run_id: str, request: Request) -> dict[str, Any]:
    """Return an owned/admin-visible run, concealing foreign run identifiers."""
    row = db.get_run(run_id)
    if row is None or not _can_access_run(row, _current_user(request)):
        raise HTTPException(404, "run not found")
    return row


def _selected_user(request: Request, requested: Optional[str]) -> Optional[str]:
    """Resolve a list/usage scope. ``None`` means all users, admin-only."""
    current = _current_user(request)
    if _is_admin(current):
        if requested is None or requested.strip().lower() in ("", "all"):
            return None
        return requested.strip().lower()
    if requested is not None and requested.strip().lower() not in ("", current):
        raise HTTPException(403, "cannot access another user's data")
    return current


def _run_public(row: dict[str, Any]) -> dict[str, Any]:
    """Run metadata shaped for the API, with args parsed back to an object."""
    out = dict(row)
    try:
        out["args"] = json.loads(row["args"])
    except (TypeError, ValueError):
        out["args"] = {}
    return out


# --------------------------------------------------------------- endpoints

@app.post("/runs", status_code=202)
async def create_run(request: Request) -> JSONResponse:
    body = await _json_body(request)
    run_type = body.get("type")
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(400, "args must be an object")

    # Validate + whitelist BEFORE reserving the slot, so a bad request never
    # blocks a good one.
    try:
        argv, clean_args = jobs.build_argv(run_type, args)
    except jobs.ArgError as e:
        raise HTTPException(400, str(e))

    # Single worker slot: reject if a job is already running/queued (plan §6).
    active = db.active_run()
    if active is not None:
        raise HTTPException(
            409,
            f"a {active['type']} run is already {active['status']}",
        )

    run_id = uuid.uuid4().hex
    db.insert_run(run_id, run_type, json.dumps(clean_args), _current_user(request))

    # Fire-and-forget supervisor; it owns the run's terminal status.
    import asyncio
    asyncio.create_task(jobs.supervise(run_id, argv))

    return JSONResponse({"id": run_id, "status": "running"}, status_code=202)


@app.get("/runs")
def list_runs(
    request: Request,
    limit: int = 100,
    user: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    selected = _selected_user(request, user)
    return {"runs": [_run_public(r) for r in db.list_runs(limit, selected)]}


@app.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    row = _authorized_run(run_id, request)
    out = _run_public(row)
    # Attach the parsed result blob once the job has finished successfully.
    result: Optional[Any] = None
    if row["status"] == "done" and row["result_path"]:
        try:
            result = json.loads(config.blob_path(run_id).read_text())
        except (OSError, ValueError):
            result = None
    out["result"] = result
    # For a failed run, surface a short stderr tail to make it diagnosable.
    if row["status"] == "failed":
        out["error_tail"] = _stderr_tail(run_id)
    return out


@app.delete("/runs/{run_id}")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    row = _authorized_run(run_id, request)
    if row["status"] not in ("queued", "running"):
        raise HTTPException(409, f"run is {row['status']}, cannot cancel")
    # Mark first so the supervisor classifies the kill as a cancel, not a failure.
    db.set_status(run_id, "cancelled")
    await jobs.cancel(run_id)
    return {"id": run_id, "status": "cancelled"}


@app.get("/health")
def health() -> dict[str, Any]:
    active = db.active_run()
    return {
        "status": "ok",
        "last_successful_run_at": db.last_successful_run_at(),
        # Health checks need availability, never a user's email, arguments, or id.
        "active_run": active is not None,
    }


@app.get("/usage")
def usage(request: Request, user: Optional[str] = Query(default=None)) -> dict[str, Any]:
    """Month-to-date *real* LLM call count (cache misses), for the cost display."""
    selected = _selected_user(request, user)
    current = _current_user(request)
    out: dict[str, Any] = {
        "month_to_date_llm_calls": db.month_to_date_llm_calls(selected),
        "scope": selected or "all",
    }
    if _is_admin(current):
        out["total_month_to_date_llm_calls"] = db.month_to_date_llm_calls()
    return out


@app.get("/session")
def session(request: Request) -> dict[str, Any]:
    """Current Access identity plus admin-only ledger filter choices."""
    current = _current_user(request)
    admin = _is_admin(current)
    out: dict[str, Any] = {"email": current, "is_admin": admin}
    if admin:
        users = set(db.list_run_users())
        users.add(current)
        out["users"] = sorted(users)
    return out


# ----------------------------------------------------------------- helpers

async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


def _stderr_tail(run_id: str, lines: int = 30) -> str:
    try:
        text = config.stderr_path(run_id).read_text()
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------- static UI

@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/runs/{run_id}/view")
def run_view(run_id: str, request: Request) -> FileResponse:
    # A dedicated result page; the run id is read client-side from the path.
    _authorized_run(run_id, request)
    return FileResponse(config.STATIC_DIR / "run.html")


# Mount remaining static assets (css/js) under /static.
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
