import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ..auth import staff
from ..auth.oidc import OidcClient, OidcError, get_oidc, pkce_pair
from ..config import get_settings
from ..db import get_db
from ..services import tenants as ts

router = APIRouter(prefix="/auth")
log = logging.getLogger("hub.auth")

PAGE = ("<!doctype html><meta charset=utf-8><title>Citichoice Desk Hub</title>"
        "<body style='font:15px system-ui;max-width:32em;margin:15vh auto;padding:0 1em'><h2>{h}</h2><p>{p}</p>{a}")


def _login_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="oidc-login")


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/ui") and "//" not in n and "\\" not in n else "/ui"


def _set(response, name: str, value: str, max_age: int):
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/login")
def login(next: str | None = None, oidc: OidcClient = Depends(get_oidc)):
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    try:
        url = oidc.authorization_url(state, nonce, challenge)
    except OidcError as e:
        log.error("oidc discovery problem: %s", e)
        return HTMLResponse(PAGE.format(h="Sign-in unavailable", p="The identity provider could not be reached.", a=""), 503)
    r = RedirectResponse(url, status_code=303)
    _set(r, staff.LOGIN_COOKIE, _login_serializer().dumps({"s": state, "n": nonce, "v": verifier, "next": _safe_next(next)}), 600)
    return r


@router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
             db: Session = Depends(get_db), oidc: OidcClient = Depends(get_oidc)):
    def deny(reason: str, status: int = 403):
        log.warning("staff login refused: %s", reason)          # reasons never include tokens or claim values
        ts.audit(db, "anonymous", "staff.login", None, {"reason": reason}, "failure")
        db.commit()
        r = HTMLResponse(PAGE.format(h="Access denied", p="You cannot sign in to Desk Hub with this account.",
                                     a="<p><a href='/auth/login'>Try again</a></p>"), status)
        r.delete_cookie(staff.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return r

    raw = request.cookies.get(staff.LOGIN_COOKIE)
    if error or not code or not state or not raw:
        return deny("missing or errored callback", 400)
    try:
        saved = _login_serializer().loads(raw, max_age=600)
    except BadSignature:
        return deny("login cookie invalid or expired", 400)
    if not secrets.compare_digest(saved["s"], state):
        return deny("state mismatch", 400)
    try:
        claims = oidc.verify_id_token(oidc.exchange_code(code, saved["v"]), saved["n"])
    except OidcError as e:
        return deny(str(e))
    roles = oidc.roles(claims)
    need = get_settings().oidc_required_role
    if need not in roles:
        log.warning("role claim names present: %s", sorted(k for k in claims if k.startswith("urn:zitadel")))
        return deny("required role missing")
    token = staff.create_session(db, claims["sub"], claims.get("email"), claims.get("name"), sorted(roles))
    ts.audit(db, claims.get("email") or claims["sub"], "staff.login", None, {"sub": claims["sub"]})
    db.commit()
    r = RedirectResponse(saved["next"], status_code=303)
    _set(r, staff.COOKIE, token, get_settings().session_absolute_hours * 3600)
    r.delete_cookie(staff.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return r


@router.post("/logout")
def logout(request: Request, csrf: str = Form(...), user: staff.StaffUser = Depends(staff.staff_session),
           db: Session = Depends(get_db), oidc: OidcClient = Depends(get_oidc)):
    staff.check_csrf(user, csrf)
    staff.revoke(db, request.cookies[staff.COOKIE])
    ts.audit(db, str(user), "staff.logout", None, {})
    db.commit()
    try:
        target = oidc.logout_url() or "/auth/logged-out"
    except OidcError:
        target = "/auth/logged-out"
    r = RedirectResponse(target, status_code=303)
    r.delete_cookie(staff.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return r


@router.get("/logged-out", response_class=HTMLResponse)
def logged_out():
    return PAGE.format(h="Signed out", p="You have been signed out of Desk Hub.", a="<p><a href='/auth/login'>Sign in</a></p>")
