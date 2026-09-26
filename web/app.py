"""Valence job-runner API and static UI, protected by Arboretum Accounts."""

from __future__ import annotations

import html
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from arboretum_auth.fastapi_integration import FastAPIAuth
from arboretum_auth.user import AuthenticatedUser
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, jobs
from .auth import account_url, load_auth, login_url, wants_html


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    yield


def _entitlement_page(title: str, message: str, action_url: str | None = None) -> str:
    action = ""
    if action_url:
        action = (
            f'<a class="action" href="{html.escape(action_url, quote=True)}">'
            "Manage account</a>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} - Valence</title>
<link rel="stylesheet" href="/static/style.css?v=accounts-1"></head>
<body><main class="auth-state"><span class="eyebrow coral">Arboretum Investments</span>
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{action}</main></body></html>"""


def create_app(auth: FastAPIAuth | None = None) -> FastAPI:
    app = FastAPI(title="Valence", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.auth = auth or load_auth()

    require_max = app.state.auth.require_user(min_plan="max")
    # arboretum-auth builds this dependency with postponed annotations inside a
    # local import scope. Current FastAPI needs the concrete types restored.
    require_max.__annotations__["request"] = Request
    require_max.__annotations__["return"] = AuthenticatedUser

    def current_user(
        authenticated: AuthenticatedUser = Depends(require_max),
    ) -> AuthenticatedUser:
        # Legacy rows are claimed exactly once by verified email. Rows that
        # already have an account ID are never rewritten.
        db.claim_legacy_runs(authenticated.id, authenticated.email)
        return authenticated

    def is_admin(user: AuthenticatedUser) -> bool:
        return bool(config.ADMIN_ACCOUNT_ID) and user.id == config.ADMIN_ACCOUNT_ID

    def can_access_run(row: dict[str, Any], user: AuthenticatedUser) -> bool:
        return is_admin(user) or row.get("account_id") == user.id

    def authorized_run(run_id: str, user: AuthenticatedUser) -> dict[str, Any]:
        """Return an owned/admin-visible run, concealing foreign identifiers."""
        row = db.get_run(run_id)
        if row is None or not can_access_run(row, user):
            raise HTTPException(404, "run not found")
        return row

    def selected_account(
        user: AuthenticatedUser, requested: Optional[str]
    ) -> Optional[str]:
        """Resolve list/usage scope; ``None`` means all accounts, admin-only."""
        if is_admin(user):
            if requested is None or requested.strip().lower() in ("", "all"):
                return None
            return requested.strip()
        if requested is not None and requested.strip() not in ("", user.id):
            raise HTTPException(403, "cannot access another account's data")
        return user.id

    @app.middleware("http")
    async def private_response_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path != "/health" and not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie"
        return response

    @app.exception_handler(HTTPException)
    async def auth_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            location = login_url(request, app.state.auth)
            if wants_html(request):
                return RedirectResponse(location, status_code=302)
            return JSONResponse(
                {
                    "error": "authentication_required",
                    "detail": "A valid Arboretum session is required.",
                    "login_url": location,
                },
                status_code=401,
                headers={"Location": location},
            )
        if exc.status_code == 403 and exc.detail == "plan upgrade required":
            upgrade = account_url(app.state.auth)
            if wants_html(request):
                return HTMLResponse(
                    _entitlement_page(
                        "Valence requires Max",
                        "Valence is available only with an Arboretum Max plan.",
                        upgrade,
                    ),
                    status_code=403,
                )
            return JSONResponse(
                {
                    "error": "max_required",
                    "detail": "Valence requires an Arboretum Max plan.",
                    "upgrade_url": upgrade,
                },
                status_code=403,
            )
        if exc.status_code == 503 and exc.detail == "auth temporarily unavailable":
            if wants_html(request):
                return HTMLResponse(
                    _entitlement_page(
                        "Authentication temporarily unavailable",
                        "Valence could not verify your session. Please try again shortly.",
                    ),
                    status_code=503,
                )
            return JSONResponse(
                {
                    "error": "authentication_unavailable",
                    "detail": "Session verification is temporarily unavailable.",
                },
                status_code=503,
            )
        return await http_exception_handler(request, exc)

    @app.post("/runs", status_code=202)
    async def create_run(
        request: Request,
        user: AuthenticatedUser = Depends(current_user),
    ) -> JSONResponse:
        body = await _json_body(request)
        run_type = body.get("type")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise HTTPException(400, "args must be an object")

        try:
            argv, clean_args = jobs.build_argv(run_type, args)
        except jobs.ArgError as exc:
            raise HTTPException(400, str(exc))

        active = db.active_run()
        if active is not None:
            raise HTTPException(
                409,
                f"a {active['type']} run is already {active['status']}",
            )

        run_id = uuid.uuid4().hex
        db.insert_run(run_id, run_type, json.dumps(clean_args), user.id, user.email)

        import asyncio
        asyncio.create_task(jobs.supervise(run_id, argv))
        return JSONResponse({"id": run_id, "status": "running"}, status_code=202)

    @app.get("/runs")
    def list_runs(
        limit: int = 100,
        requested: Optional[str] = Query(default=None, alias="user"),
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        selected = selected_account(user, requested)
        return {"runs": [_run_public(row) for row in db.list_runs(limit, selected)]}

    @app.get("/runs/{run_id}")
    def get_run(
        run_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        row = authorized_run(run_id, user)
        out = _run_public(row)
        result: Optional[Any] = None
        if row["status"] == "done" and row["result_path"]:
            try:
                result = json.loads(config.blob_path(run_id).read_text())
            except (OSError, ValueError):
                result = None
        out["result"] = result
        if row["status"] == "failed":
            out["error_tail"] = _stderr_tail(run_id)
        return out

    @app.delete("/runs/{run_id}")
    async def cancel_run(
        run_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        row = authorized_run(run_id, user)
        if row["status"] not in ("queued", "running"):
            raise HTTPException(409, f"run is {row['status']}, cannot cancel")
        db.set_status(run_id, "cancelled")
        await jobs.cancel(run_id)
        return {"id": run_id, "status": "cancelled"}

    @app.get("/health")
    def health() -> dict[str, Any]:
        active = db.active_run()
        return {
            "status": "ok",
            "last_successful_run_at": db.last_successful_run_at(),
            "active_run": active is not None,
        }

    @app.get("/usage")
    def usage(
        requested: Optional[str] = Query(default=None, alias="user"),
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        selected = selected_account(user, requested)
        out: dict[str, Any] = {
            "month_to_date_llm_calls": db.month_to_date_llm_calls(selected),
            "scope": selected or "all",
        }
        if is_admin(user):
            out["total_month_to_date_llm_calls"] = db.month_to_date_llm_calls()
        return out

    @app.get("/session")
    def session(user: AuthenticatedUser = Depends(current_user)) -> dict[str, Any]:
        admin = is_admin(user)
        out: dict[str, Any] = {
            "email": user.email,
            "plan": user.plan.value,
            "is_admin": admin,
            "account_url": account_url(app.state.auth),
        }
        if admin:
            accounts = {row["account_id"]: row for row in db.list_run_accounts()}
            accounts[user.id] = {"account_id": user.id, "email": user.email}
            out["users"] = sorted(
                accounts.values(), key=lambda row: (row["email"], row["account_id"])
            )
        return out

    @app.get("/")
    def index(user: AuthenticatedUser = Depends(current_user)) -> FileResponse:
        return FileResponse(config.STATIC_DIR / "index.html")

    @app.get("/runs/{run_id}/view")
    def run_view(
        run_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> FileResponse:
        authorized_run(run_id, user)
        return FileResponse(config.STATIC_DIR / "run.html")

    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
    return app


def _run_public(row: dict[str, Any]) -> dict[str, Any]:
    """Shape run metadata for the API without exposing stable account IDs."""
    out = dict(row)
    out.pop("account_id", None)
    try:
        out["args"] = json.loads(row["args"])
    except (TypeError, ValueError):
        out["args"] = {}
    return out


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


_app = None


def __getattr__(name: str):
    """Construct production app lazily so tests can inject an offline verifier."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
