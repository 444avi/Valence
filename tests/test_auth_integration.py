"""Signed-session verification and Max entitlement behavior."""

import importlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from arboretum_auth.errors import ConfigurationError, VerificationUnavailable
from arboretum_auth.fastapi_integration import FastAPIAuth
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.auth_support import VALENCE_ORIGIN, auth_config, auth_for, auth_raising


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VALENCE_HOME", str(tmp_path))
    monkeypatch.setenv("VALENCE_ADMIN_ACCOUNT_ID", "acct-admin")
    from web import app as app_mod, config, db, jobs

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(jobs)
    importlib.reload(app_mod)
    db.init_db()
    return app_mod


@pytest.fixture(scope="module")
def signing_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, other_key, public_pem


def token(
    key,
    *,
    account_id="acct-customer",
    email="customer@example.com",
    plan="max",
    issuer="https://accounts.arboretuminvestments.net",
    audience="arboretum-tools",
    expires_delta=timedelta(minutes=10),
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": account_id,
            "wos_user_id": f"wos-{account_id}",
            "email": email,
            "plan": plan,
            "sid": "session-test",
            "iat": now,
            "exp": now + expires_delta,
        },
        key,
        algorithm="RS256",
    )


def real_client(app_env, public_pem) -> TestClient:
    auth = FastAPIAuth(auth_config(public_key=public_pem))
    return TestClient(
        app_env.create_app(auth=auth),
        base_url=VALENCE_ORIGIN,
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    "kind", ["missing", "malformed", "invalid_signature", "expired", "issuer", "audience"]
)
def test_invalid_sessions_are_rejected(app_env, signing_keys, kind):
    private_key, other_key, public_pem = signing_keys
    client = real_client(app_env, public_pem)
    value = None
    if kind == "malformed":
        value = "not-a-jwt"
    elif kind == "invalid_signature":
        value = token(other_key)
    elif kind == "expired":
        value = token(private_key, expires_delta=timedelta(minutes=-2))
    elif kind == "issuer":
        value = token(private_key, issuer="https://attacker.example")
    elif kind == "audience":
        value = token(private_key, audience="some-other-tool")

    cookies = {"arb_session": value} if value else None
    response = client.get("/session", cookies=cookies)
    assert response.status_code == 401
    assert response.json()["error"] == "authentication_required"
    assert response.headers["location"].startswith(
        "https://accounts.arboretuminvestments.net/refresh?return_to="
    )


def test_html_login_redirect_preserves_exact_deep_link(app_env, signing_keys):
    _, _, public_pem = signing_keys
    response = real_client(app_env, public_pem).get(
        "/runs/abc/view?tab=matched&sort=profit",
        headers={"accept": "text/html"},
    )
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "accounts.arboretuminvestments.net",
        "/refresh",
    )
    assert parse_qs(location.query)["return_to"] == [
        f"{VALENCE_ORIGIN}/runs/abc/view?tab=matched&sort=profit"
    ]


def test_api_401_is_machine_readable_and_ignores_access_header(app_env, signing_keys):
    _, _, public_pem = signing_keys
    response = real_client(app_env, public_pem).get(
        "/runs",
        headers={"Cf-Access-Authenticated-User-Email": "avi@arboretuminvestments.net"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "authentication_required"
    assert response.json()["login_url"] == response.headers["location"]
    assert response.headers["vary"] == "Cookie"


def test_api_401_returns_to_page_not_api_endpoint(app_env, signing_keys):
    _, _, public_pem = signing_keys
    client = real_client(app_env, public_pem)

    def return_to(headers):
        response = client.get("/runs?limit=50", headers=headers)
        assert response.status_code == 401
        return parse_qs(urlsplit(response.headers["location"]).query)["return_to"]

    assert return_to({}) == [f"{VALENCE_ORIGIN}/"]
    assert return_to({"referer": f"{VALENCE_ORIGIN}/runs/abc/view"}) == [
        f"{VALENCE_ORIGIN}/runs/abc/view"
    ]
    assert return_to({"referer": f"{VALENCE_ORIGIN}/runs?limit=50"}) == [
        f"{VALENCE_ORIGIN}/"
    ]
    assert return_to({"referer": "https://evil.example/runs/abc/view"}) == [
        f"{VALENCE_ORIGIN}/"
    ]


def test_verification_outage_returns_503_without_destroying_cookie(app_env):
    client = TestClient(
        app_env.create_app(auth=auth_raising(VerificationUnavailable("JWKS offline"))),
        base_url=VALENCE_ORIGIN,
        follow_redirects=False,
    )
    client.cookies.set("arb_session", "still-valid-until-verified")
    response = client.get("/session")
    assert response.status_code == 503
    assert response.json()["error"] == "authentication_unavailable"
    assert "set-cookie" not in response.headers
    assert client.cookies.get("arb_session") == "still-valid-until-verified"


@pytest.mark.parametrize("plan", ["free", "premium"])
def test_non_max_plans_receive_upgrade_response_and_cannot_create_runs(app_env, plan):
    client = TestClient(
        app_env.create_app(auth=auth_for(f"{plan}@example.com", plan=plan)),
        base_url=VALENCE_ORIGIN,
        follow_redirects=False,
    )
    html = client.get("/", headers={"accept": "text/html"})
    assert html.status_code == 403
    assert "Valence requires Max" in html.text
    assert "accounts.arboretuminvestments.net/account?return_to=" in html.text
    assert "location" not in html.headers  # no login/upgrade redirect loop

    api = client.post("/runs", json={"type": "scan", "args": {"no_llm": True}})
    assert api.status_code == 403
    assert api.json()["error"] == "max_required"
    assert api.json()["upgrade_url"].startswith(
        "https://accounts.arboretuminvestments.net/account?return_to="
    )


def test_max_plan_can_open_ui_and_session(app_env):
    client = TestClient(
        app_env.create_app(auth=auth_for("max@example.com", plan="max")),
        base_url=VALENCE_ORIGIN,
    )
    assert client.get("/").status_code == 200
    assert client.get("/session").json()["plan"] == "max"


def test_health_and_static_are_public(app_env, signing_keys):
    _, _, public_pem = signing_keys
    client = real_client(app_env, public_pem)
    assert client.get("/health").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_production_auth_configuration_fails_closed(monkeypatch):
    from web.auth import _PRODUCTION_ENV, load_auth

    for name, value in _PRODUCTION_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ARBORETUM_JWKS_URL")
    with pytest.raises(ConfigurationError, match="ARBORETUM_JWKS_URL"):
        load_auth()

    monkeypatch.setenv("ARBORETUM_JWKS_URL", _PRODUCTION_ENV["ARBORETUM_JWKS_URL"])
    monkeypatch.setenv("ARBORETUM_AUDIENCE", "wrong-audience")
    with pytest.raises(ConfigurationError, match="ARBORETUM_AUDIENCE"):
        load_auth()
