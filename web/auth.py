"""Valence's first-party Arboretum authentication boundary."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit

from arboretum_auth import AuthConfig
from arboretum_auth.errors import ConfigurationError
from arboretum_auth.fastapi_integration import FastAPIAuth
from arboretum_auth.redirects import build_login_url
from fastapi import Request


ACCOUNTS_ORIGIN = "https://accounts.arboretuminvestments.net"
VALENCE_ORIGIN = "https://valence.arboretuminvestments.net"

_PRODUCTION_ENV = {
    "ARBORETUM_ACCOUNTS_URL": ACCOUNTS_ORIGIN,
    "ARBORETUM_ISSUER": ACCOUNTS_ORIGIN,
    "ARBORETUM_AUDIENCE": "arboretum-tools",
    "ARBORETUM_JWKS_URL": f"{ACCOUNTS_ORIGIN}/.well-known/jwks.json",
    "ARBORETUM_COOKIE_NAME": "arb_session",
    "ARBORETUM_ALLOWED_RETURN_HOSTS": "valence.arboretuminvestments.net",
    "ARBORETUM_RETURN_ORIGIN": VALENCE_ORIGIN,
}


def load_auth() -> FastAPIAuth:
    """Create the production verifier and reject partial or unsafe config."""
    for name, expected in _PRODUCTION_ENV.items():
        actual = os.environ.get(name, "").strip()
        if actual.rstrip("/") != expected.rstrip("/"):
            raise ConfigurationError(
                f"{name} is missing or inconsistent with Valence production auth"
            )
    return FastAPIAuth(AuthConfig.from_env())


_PAGE_PATH = re.compile(r"^/(?:runs/[^/]+/view)?$")


def canonical_return_to(request: Request, auth: FastAPIAuth) -> str:
    """Preserve the exact deep link on the configured public HTTPS origin.

    API calls (e.g. the dashboard's /runs poll) must never become the
    post-login destination, or the browser lands on raw JSON. For those,
    return to the page that issued the call (same-origin Referer) or "/".
    """
    origin = (auth.config.return_origin or str(request.base_url)).rstrip("/")
    if wants_html(request):
        query = request.url.query
        return f"{origin}{request.url.path}" + (f"?{query}" if query else "")
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc in (urlsplit(origin).netloc, request.url.netloc) and _PAGE_PATH.match(
        referer.path
    ):
        return f"{origin}{referer.path}" + (f"?{referer.query}" if referer.query else "")
    return f"{origin}/"


def login_url(request: Request, auth: FastAPIAuth) -> str:
    """Use Accounts refresh first so an existing SSO session can recover."""
    entrypoint = auth.config.refresh_url or auth.config.login_url
    return build_login_url(entrypoint, canonical_return_to(request, auth))


def account_url(auth: FastAPIAuth) -> str:
    origin = auth.config.return_origin or VALENCE_ORIGIN
    return (
        f"{auth.config.issuer.rstrip('/')}/account?"
        f"return_to={quote(origin + '/', safe='')}"
    )


def wants_html(request: Request) -> bool:
    """Recognize navigations while keeping API/XHR failures machine readable."""
    if "text/html" in request.headers.get("accept", ""):
        return True
    path = request.url.path
    return path == "/" or path.endswith("/view")
