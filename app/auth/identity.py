"""Customer identity. The intended production provider is Citichoice account SSO (OIDC, Logto planned).
That protocol is NOT verified yet, so no production adapter exists. See docs/SSO_NOTES.md."""
from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request

from ..config import get_settings


@dataclass(frozen=True)
class Identity:
    subject: str                 # the IdP's stable user id ("sub")
    email: str | None = None
    email_verified: bool = False


class IdentityProvider(Protocol):
    def authenticate(self, request: Request) -> Identity: ...


class DevHeaderIdentityProvider:
    """DEVELOPMENT AND TESTS ONLY. Trusts headers, so it must never run in production."""
    def authenticate(self, request: Request) -> Identity:
        sub = request.headers.get("x-dev-subject")
        if not sub:
            raise HTTPException(401, {"code": "unauthenticated", "message": "missing identity"})
        return Identity(subject=sub, email=request.headers.get("x-dev-email"), email_verified=True)


def get_identity(request: Request) -> Identity:
    s = get_settings()
    if s.env in ("development", "test"):
        return DevHeaderIdentityProvider().authenticate(request)
    # Production: wire the verified OIDC adapter here once the SSO protocol is confirmed.
    raise HTTPException(503, {"code": "sso_not_configured", "message": "customer sign-in is not enabled yet"})
