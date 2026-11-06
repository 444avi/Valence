"""Offline Arboretum-auth fixtures shared by Valence web tests."""

from datetime import datetime, timedelta, timezone

from arboretum_auth import AuthConfig, AuthenticatedUser, Plan
from arboretum_auth.fastapi_integration import FastAPIAuth


ACCOUNTS_ORIGIN = "https://accounts.arboretuminvestments.net"
VALENCE_ORIGIN = "https://valence.arboretuminvestments.net"


class StaticVerifier:
    def __init__(self, user: AuthenticatedUser):
        self.user = user

    def verify(self, token: str) -> AuthenticatedUser:
        return self.user


class RaisingVerifier:
    def __init__(self, error: Exception):
        self.error = error

    def verify(self, token: str) -> AuthenticatedUser:
        raise self.error


def auth_config(*, public_key: str = "unused-by-test-verifier") -> AuthConfig:
    return AuthConfig(
        issuer=ACCOUNTS_ORIGIN,
        login_url=f"{ACCOUNTS_ORIGIN}/login",
        refresh_url=f"{ACCOUNTS_ORIGIN}/refresh",
        audience="arboretum-tools",
        cookie_name="arb_session",
        public_key=public_key,
        allowed_return_hosts=("valence.arboretuminvestments.net",),
        return_origin=VALENCE_ORIGIN,
    )


def auth_raising(error: Exception) -> FastAPIAuth:
    return FastAPIAuth(auth_config(), verifier=RaisingVerifier(error))


def auth_for(
    email: str,
    *,
    account_id: str | None = None,
    plan: str = "max",
) -> FastAPIAuth:
    now = datetime.now(timezone.utc)
    user = AuthenticatedUser(
        id=account_id or f"acct-{email.strip().casefold()}",
        workos_user_id=f"wos-{account_id or email.strip().casefold()}",
        email=email.strip().casefold(),
        plan=Plan.coerce(plan),
        session_id="test-session",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    return FastAPIAuth(auth_config(), verifier=StaticVerifier(user))
