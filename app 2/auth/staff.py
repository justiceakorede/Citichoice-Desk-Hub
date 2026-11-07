"""Staff sessions: random cookie token, server-side row, role checked on every request."""
import datetime as dt
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import StaffSession, now

COOKIE = "__Host-deskhub_session"
LOGIN_COOKIE = "__Host-deskhub_login"


class StaffUser(str):
    """Behaves as the display label (email or subject) but carries the session facts the UI needs."""
    sub: str
    csrf: str
    roles: list

    def __new__(cls, label: str, sub: str, csrf: str, roles: list):
        o = super().__new__(cls, label)
        o.sub, o.csrf, o.roles = sub, csrf, roles
        return o


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d


def create_session(db: Session, sub: str, email: str | None, name: str | None, roles: list[str]) -> str:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    n = now()
    db.add(StaffSession(id=_hash(token), sub=sub, email=email, name=name, roles=sorted(roles),
                        csrf_token=secrets.token_urlsafe(24), created_at=n, last_seen_at=n,
                        expires_at=n + dt.timedelta(hours=s.session_absolute_hours)))
    return token


def revoke(db: Session, token: str) -> None:
    row = db.get(StaffSession, _hash(token))
    if row:
        row.revoked = True


def _redirect_or_401(request: Request) -> HTTPException:
    if request.method == "GET":
        return HTTPException(303, "sign in required", headers={"Location": f"/auth/login?next={request.url.path}"})
    return HTTPException(401, {"code": "unauthenticated", "message": "sign in required"})


def staff_session(request: Request, db: Session = Depends(get_db)) -> StaffUser:
    """Every /ui route depends on this. Authentication, expiry and the administrator role are all enforced here."""
    s = get_settings()
    token = request.cookies.get(COOKIE)
    if not token:
        raise _redirect_or_401(request)
    row = db.get(StaffSession, _hash(token))
    n = now()
    if (not row or row.revoked or _aware(row.expires_at) <= n
            or _aware(row.last_seen_at) + dt.timedelta(minutes=s.session_idle_minutes) <= n):
        raise _redirect_or_401(request)
    if s.oidc_required_role not in (row.roles or []):
        raise HTTPException(403, {"code": "forbidden", "message": "administrator role required"})
    if (n - _aware(row.last_seen_at)).total_seconds() > 60:       # throttle writes
        row.last_seen_at = n
        db.commit()
    return StaffUser(row.email or row.sub, row.sub, row.csrf_token, list(row.roles or []))


def check_csrf(user: StaffUser, token: str) -> None:
    if not token or not hmac.compare_digest(token, user.csrf):
        raise HTTPException(403, {"code": "bad_csrf", "message": "bad csrf token"})
