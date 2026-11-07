"""Staff sign-in against a MOCK identity provider.

These tests prove the hub validates what it should. They do NOT prove compatibility with the real Zitadel:
that needs docs/ZITADEL_LIVE_CHECKLIST.md, run by hand against accounts.citichoice.co."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select, text

from app import db as database
from app.auth import staff
from app.auth.oidc import OidcClient, get_oidc
from app.config import get_settings
from app.main import app
from app.models import AuditLog, StaffSession, now
from .conftest import ADMIN, H, csrf_of, login_as

ISS, CID, PROJ = "https://idp.test", "client-123", "proj-1"
ROLE_CLAIM = f"urn:zitadel:iam:org:project:{PROJ}:roles"


class FakeIdp:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
        self.nonce = "unset"
        self.overrides, self.drop = {}, set()
        self.sign_key, self.alg = self.key, "RS256"
        self.token_requests = []
        self.token_status = 200

    def claims(self):
        n = int(time.time())
        c = {"iss": ISS, "sub": "user-1", "aud": [CID, PROJ], "azp": CID, "iat": n, "exp": n + 300, "nonce": self.nonce,
             "email": "admin@citichoice.test", "name": "Admin One", ROLE_CLAIM: {"administrator": {"org1": "citichoice.co"}}}
        c.update(self.overrides)
        for k in self.drop:
            c.pop(k, None)
        return c

    def token(self):
        return jwt.encode({"alg": self.alg, "kid": "k1"}, self.claims(), self.sign_key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = request.url.path
        if u == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": f"{ISS}/oauth/v2/authorize",
                "token_endpoint": f"{ISS}/oauth/v2/token", "jwks_uri": f"{ISS}/oauth/v2/keys",
                "end_session_endpoint": f"{ISS}/oidc/v1/end_session", "code_challenge_methods_supported": ["S256"]})
        if u == "/oauth/v2/keys":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if u == "/oauth/v2/token":
            self.token_requests.append(request)
            return httpx.Response(self.token_status, json={"id_token": self.token(), "access_token": "AT-secret", "refresh_token": "RT-secret"})
        return httpx.Response(404)


@pytest.fixture()
def idp(client):
    f = FakeIdp()
    oidc = OidcClient(get_settings(), http=httpx.Client(transport=httpx.MockTransport(f.handler)))
    app.dependency_overrides[get_oidc] = lambda: oidc
    f.oidc = oidc
    return f


@pytest.fixture()
def browser(client):
    return TestClient(app, base_url="https://deskhub.test")


def begin(browser, idp, nxt=None):
    r = browser.get("/auth/login" + (f"?next={nxt}" if nxt else ""), follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    idp.nonce = q["nonce"][0]
    return r, q


def finish(browser, q, code="code-1", state=None):
    return browser.get(f"/auth/callback?code={code}&state={state or q['state'][0]}", follow_redirects=False)


def sessions():
    with database.SessionLocal() as s:
        return s.scalars(select(StaffSession)).all()


# ---------------------------------------------------------------- the happy path and what it must set
def test_login_redirect_is_correct(browser, idp):
    r, q = begin(browser, idp)
    assert r.headers["location"].startswith(f"{ISS}/oauth/v2/authorize?")
    assert q["client_id"] == [CID] and q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://deskhub.test/auth/callback"]
    assert {"openid", "email", "urn:zitadel:iam:org:project:roles", f"urn:zitadel:iam:org:project:id:{PROJ}:aud"} <= set(q["scope"][0].split())
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "Secure" in sc and "SameSite=lax" in sc and sc.startswith(staff.LOGIN_COOKIE)


def test_successful_login_creates_a_server_side_session(browser, idp):
    _, q = begin(browser, idp)
    r = finish(browser, q)
    assert r.status_code == 303 and r.headers["location"] == "/ui"
    sc = [h for h in r.headers.get_list("set-cookie") if h.startswith(staff.COOKIE)][0]
    assert "HttpOnly" in sc and "Secure" in sc and "SameSite=lax" in sc and "Path=/" in sc and "Domain" not in sc
    rows = sessions()
    assert len(rows) == 1 and rows[0].roles == ["administrator"] and rows[0].email == "admin@citichoice.test"
    cookie = browser.cookies.get(staff.COOKIE)
    assert rows[0].id == hashlib.sha256(cookie.encode()).hexdigest() and cookie not in rows[0].id   # only the hash is stored
    assert browser.get("/ui").status_code == 200


def test_token_endpoint_call_uses_basic_auth_and_pkce_and_no_token_is_stored(browser, idp):
    _, q = begin(browser, idp)
    finish(browser, q)
    req = idp.token_requests[0]
    assert req.headers["authorization"].startswith("Basic ")
    assert base64.b64decode(req.headers["authorization"][6:]).decode() == f"{CID}:s3cret-for-tests"
    body = parse_qs(req.content.decode())
    assert body["grant_type"] == ["authorization_code"] and len(body["code_verifier"][0]) >= 43
    with database.engine.connect() as c:
        for t in ("staff_sessions", "audit_log"):
            dump = str(c.execute(text(f"select * from {t}")).fetchall())
            assert "AT-secret" not in dump and "RT-secret" not in dump and "s3cret" not in dump and "eyJ" not in dump


def test_login_is_audited(browser, idp):
    _, q = begin(browser, idp)
    finish(browser, q)
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "staff.login")).one()
    assert row.outcome == "success" and row.actor == "admin@citichoice.test"


# ---------------------------------------------------------------- everything that must be refused
def refused(browser, r):
    assert r.status_code in (400, 403) and sessions() == []
    assert browser.get("/ui", follow_redirects=False).status_code == 303          # still signed out


def test_state_mismatch_refused(browser, idp):
    _, q = begin(browser, idp)
    refused(browser, finish(browser, q, state="attacker-state"))


def test_missing_or_tampered_login_cookie_refused(browser, idp):
    _, q = begin(browser, idp)
    browser.cookies.clear()
    refused(browser, finish(browser, q))
    _, q = begin(browser, idp)
    browser.cookies.set(staff.LOGIN_COOKIE, browser.cookies.get(staff.LOGIN_COOKIE)[:-3] + "xyz", domain="deskhub.test")
    refused(browser, finish(browser, q))


def test_idp_error_or_missing_code_refused(browser, idp):
    begin(browser, idp)
    refused(browser, browser.get("/auth/callback?error=access_denied&state=x", follow_redirects=False))


def test_code_exchange_failure_refused(browser, idp):
    _, q = begin(browser, idp)
    idp.token_status = 400
    refused(browser, finish(browser, q))


@pytest.mark.parametrize("override", [
    {"iss": "https://evil.test"}, {"aud": ["someone-else", PROJ]}, {"aud": [CID]},                   # wrong issuer / audience / not our project
    {"exp": 1}, {"nonce": "replayed-nonce"}, {"aud": [CID, PROJ, "x"], "azp": "other-client"},
], ids=["issuer", "audience", "no-project-aud", "expired", "nonce", "azp"])
def test_bad_claims_refused(browser, idp, override):
    _, q = begin(browser, idp)
    idp.overrides = override
    if "nonce" in override:
        idp.overrides = {"nonce": "replayed-nonce"}
    refused(browser, finish(browser, q))


@pytest.mark.parametrize("drop", ["sub", "exp", "iat", "iss", "aud"])
def test_missing_required_claims_refused(browser, idp, drop):
    _, q = begin(browser, idp)
    idp.drop = {drop}
    refused(browser, finish(browser, q))


def test_token_signed_by_another_key_refused(browser, idp):
    _, q = begin(browser, idp)
    idp.sign_key = RSAKey.generate_key(2048, parameters={"kid": "k1"})          # same kid, attacker's key
    refused(browser, finish(browser, q))


def test_hs256_algorithm_confusion_refused(browser, idp):
    _, q = begin(browser, idp)
    idp.token = lambda: _forge_hs256(idp)             # classic attack: HMAC-sign with the IdP's PUBLIC key as the secret
    refused(browser, finish(browser, q))


def _forge_hs256(idp):
    import hmac
    b = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=")
    head = b(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
    body = b(json.dumps(idp.claims()).encode())
    sig = b(hmac.new(idp.key.as_pem(private=False), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


def test_alg_none_refused(browser, idp):
    _, q = begin(browser, idp)
    b = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=")
    idp.token = lambda: (b(json.dumps({"alg": "none", "kid": "k1"}).encode()) + b"." + b(json.dumps(idp.claims()).encode()) + b".").decode()
    refused(browser, finish(browser, q))


def test_user_without_the_administrator_role_refused(browser, idp):
    _, q = begin(browser, idp)
    idp.overrides = {ROLE_CLAIM: {"viewer": {"org1": "x"}}}
    refused(browser, finish(browser, q))
    with database.SessionLocal() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "staff.login", AuditLog.outcome == "failure")).first()


def test_user_with_no_role_claim_at_all_refused(browser, idp):
    _, q = begin(browser, idp)
    idp.drop = {ROLE_CLAIM}
    refused(browser, finish(browser, q))


def test_role_from_another_project_claim_does_not_count(browser, idp):
    _, q = begin(browser, idp)
    idp.drop = {ROLE_CLAIM}
    idp.overrides = {"urn:zitadel:iam:org:project:other-project:roles": {"administrator": {"o": "x"}}}
    refused(browser, finish(browser, q))


def test_login_cookie_cannot_be_replayed_after_success(browser, idp):
    _, q = begin(browser, idp)
    saved = browser.cookies.get(staff.LOGIN_COOKIE)
    assert finish(browser, q).status_code == 303
    browser.cookies.set(staff.LOGIN_COOKIE, saved, domain="deskhub.test")
    idp.nonce = "x"
    assert finish(browser, q).status_code in (400, 403)                           # nonce no longer matches


def test_open_redirect_is_ignored(browser, idp):
    for nxt in ("https://evil.test", "//evil.test", "/admin/tenants", "/ui/../x//y"):
        _, q = begin(browser, idp, nxt)
        r = finish(browser, q)
        assert r.headers["location"] == "/ui"
        browser.cookies.clear()


def test_next_inside_ui_is_kept(browser, idp):
    _, q = begin(browser, idp, "/ui/rates")
    assert finish(browser, q).headers["location"] == "/ui/rates"


def test_key_rotation_is_handled(browser, idp):
    _, q = begin(browser, idp)
    idp.oidc._keyset()                                                            # old keys cached
    idp.oidc._jwks_at -= 3600
    idp.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})   # IdP rotates
    idp.sign_key = idp.key
    assert finish(browser, q).status_code == 303


# ---------------------------------------------------------------- authorization on the protected server side
UI_ROUTES = [("GET", "/ui"), ("GET", "/ui/rates"), ("GET", "/ui/audit"), ("GET", "/ui/tenants/x"),
             ("POST", "/ui/tenants/x/adjustments"), ("POST", "/ui/adjustments/x/approve"), ("POST", "/ui/adjustments/x/reject"), ("GET", "/ui/adjustments"), ("POST", "/ui/tenants/x/suspend"), ("POST", "/ui/tenants/x/unsuspend"),
             ("POST", "/ui/rates"), ("POST", "/ui/fx"), ("POST", "/auth/logout")]


@pytest.mark.parametrize("method,path", UI_ROUTES)
def test_unauthenticated_requests_never_reach_the_handlers(client, browser, method, path):
    r = browser.request(method, path, data={"csrf": "x"}, follow_redirects=False)
    assert (r.status_code == 303 and r.headers["location"].startswith("/auth/login")) if method == "GET" else r.status_code == 401


@pytest.mark.parametrize("method,path", UI_ROUTES)
def test_signed_in_user_without_the_role_gets_403_everywhere(client, browser, method, path):
    login_as(browser, roles=("viewer",))
    r = browser.request(method, path, data={"csrf": csrf_of(browser)}, follow_redirects=False)
    assert r.status_code == 403


def test_role_is_rechecked_each_request_not_only_at_login(client, staff_client, monkeypatch):
    assert staff_client.get("/ui").status_code == 200
    monkeypatch.setattr(get_settings(), "oidc_required_role", "finance_admin")
    assert staff_client.get("/ui").status_code == 403


def test_state_changing_actions_need_a_valid_csrf_token_and_origin(client, staff_client, tenant):
    path = f"/ui/tenants/{tenant}/adjustments"
    form = {"kind": "wallet_credit", "amount": "10", "reason": "test credit", "evidence": "TKT-1"}
    assert staff_client.post(path, data={**form, "csrf": "wrong"}).status_code == 403
    assert staff_client.post(path, data={**form, "csrf": csrf_of(staff_client)}, headers={"origin": "https://evil.test"}).status_code == 403
    r = staff_client.post(path, data={**form, "csrf": csrf_of(staff_client)}, headers={"origin": "https://deskhub.test"}, follow_redirects=False)
    assert r.status_code == 303


def test_ui_actions_are_attributed_to_the_signed_in_person(client, staff_client, tenant):
    staff_client.post(f"/ui/tenants/{tenant}/suspend", data={"mode": "pause_outbound", "csrf": csrf_of(staff_client)})
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "tenant.ui_suspend")).one()
    assert row.actor == "admin@citichoice.test"


def test_sessions_expire(client, browser):
    login_as(browser)
    assert browser.get("/ui").status_code == 200
    import datetime as dt
    with database.SessionLocal() as s:
        row = s.scalars(select(StaffSession)).one()
        row.last_seen_at = now() - dt.timedelta(minutes=31)
        s.commit()
    assert browser.get("/ui", follow_redirects=False).status_code == 303          # idle timeout
    token = login_as(browser)
    with database.SessionLocal() as s:
        row = s.get(StaffSession, hashlib.sha256(token.encode()).hexdigest())
        row.expires_at = now() - dt.timedelta(seconds=1)
        s.commit()
    assert browser.get("/ui", follow_redirects=False).status_code == 303          # absolute timeout


def test_logout_revokes_the_session(client, staff_client, idp):
    r = staff_client.post("/auth/logout", data={"csrf": csrf_of(staff_client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISS}/oidc/v1/end_session")
    assert sessions()[0].revoked is True
    assert staff_client.get("/ui", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- the two hostnames and the machine API
def test_a_session_cookie_is_useless_on_the_machine_api(client, staff_client):
    staff_client.get("/ui")
    r = client.get("/admin/tenants", cookies={staff.COOKIE: staff_client.cookies.get(staff.COOKIE)})
    assert r.status_code == 401


def test_an_api_key_does_not_open_the_staff_ui(client, staff_client):
    r = TestClient(app, base_url="https://deskhub.test").get("/ui", headers=H(ADMIN), follow_redirects=False)
    assert r.status_code == 303


def test_hosts_only_serve_their_own_routes(client, staff_client):
    assert staff_client.get("/admin/tenants", headers=H(ADMIN)).status_code == 404       # API on the staff host
    assert staff_client.get("/internal/channels").status_code == 404
    assert client.get("/ui").status_code == 404                                           # staff UI on the API host
    assert client.get("/auth/login").status_code == 404
    assert client.get("/healthz").status_code == 200 and staff_client.get("/healthz").status_code == 200
    other = TestClient(app, base_url="https://unknown.test")
    assert other.get("/ui").status_code == 404 and other.get("/admin/tenants", headers=H(ADMIN)).status_code == 404


def test_customer_routes_are_off_by_default(client):
    assert client.get("/customer/tenants", headers={"x-dev-subject": "s"}).status_code == 404
    assert client.get("/u/anything").status_code == 404
    assert client.get("/admin/tenants/x/usage-link", headers=H(ADMIN)).status_code == 404


def test_security_headers_on_staff_pages(client, staff_client):
    r = staff_client.get("/ui")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
