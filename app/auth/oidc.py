"""Minimal, strict OpenID Connect client for Zitadel (authorization code + PKCE, confidential client).

Everything a login depends on is checked here: issuer, signature (from the IdP's JWKS, algorithm allow-list),
audience, expiry, nonce, and that the token is for this project. Nothing from the token is kept."""
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"]   # never "none" or HS*


class OidcError(Exception):
    """Login must be refused. The message is safe to log but is never shown to the user verbatim."""


@dataclass
class Endpoints:
    authorization: str
    token: str
    jwks: str
    end_session: str | None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OidcClient:
    def __init__(self, settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=10.0, follow_redirects=False)
        self._ep: Endpoints | None = None
        self._jwks = None
        self._jwks_at = 0.0

    # ---- discovery
    def endpoints(self) -> Endpoints:
        if self._ep:
            return self._ep
        iss = self.s.oidc_issuer.rstrip("/")
        try:
            doc = self.http.get(f"{iss}/.well-known/openid-configuration").json()
        except Exception as e:
            raise OidcError(f"discovery failed: {type(e).__name__}")
        if doc.get("issuer") != self.s.oidc_issuer:
            raise OidcError("discovery issuer does not match OIDC_ISSUER")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(doc.get(k, "")).startswith("https://"):
                raise OidcError(f"discovery {k} missing or not https")
        if "S256" not in doc.get("code_challenge_methods_supported", ["S256"]):
            raise OidcError("IdP does not support PKCE S256")
        self._ep = Endpoints(doc["authorization_endpoint"], doc["token_endpoint"], doc["jwks_uri"], doc.get("end_session_endpoint"))
        return self._ep

    def scopes(self) -> str:
        scopes = ["openid", "profile", "email", "urn:zitadel:iam:org:project:roles"]
        if self.s.oidc_project_id:
            scopes.append(f"urn:zitadel:iam:org:project:id:{self.s.oidc_project_id}:aud")
        return " ".join(scopes)

    # ---- step 1: send the browser to the IdP
    def authorization_url(self, state: str, nonce: str, challenge: str) -> str:
        q = {"response_type": "code", "client_id": self.s.oidc_client_id, "redirect_uri": self.s.oidc_redirect_uri,
             "scope": self.scopes(), "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.endpoints().authorization}?{urlencode(q)}"

    # ---- step 2: swap the code for tokens (server to server, client_secret_basic)
    def exchange_code(self, code: str, verifier: str) -> str:
        try:
            r = self.http.post(self.endpoints().token, auth=(self.s.oidc_client_id, self.s.oidc_client_secret),
                               data={"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": self.s.oidc_redirect_uri, "code_verifier": verifier})
        except httpx.HTTPError as e:
            raise OidcError(f"token endpoint unreachable: {type(e).__name__}")
        if r.status_code != 200:
            raise OidcError(f"token endpoint refused the code ({r.status_code})")
        id_token = r.json().get("id_token")
        if not id_token:
            raise OidcError("no id_token in token response")
        return id_token                      # access and refresh tokens are ignored and never stored

    # ---- step 3: validate the ID token
    def _keyset(self, force: bool = False):
        if force or not self._jwks or time.time() - self._jwks_at > 600:
            try:
                self._jwks = KeySet.import_key_set(self.http.get(self.endpoints().jwks).json())
            except Exception as e:
                raise OidcError(f"jwks fetch failed: {type(e).__name__}")
            self._jwks_at = time.time()
        return self._jwks

    def verify_id_token(self, id_token: str, nonce: str) -> dict:
        registry = jwt.JWTClaimsRegistry(
            leeway=60,
            iss={"essential": True, "value": self.s.oidc_issuer},
            aud={"essential": True, "value": self.s.oidc_client_id},
            exp={"essential": True}, sub={"essential": True}, iat={"essential": True})
        claims = None
        for attempt in (0, 1):                # a second try with fresh keys handles IdP key rotation
            try:
                token = jwt.decode(id_token, self._keyset(force=bool(attempt)), algorithms=ALLOWED_ALGS)
                registry.validate(token.claims)
                claims = token.claims
                break
            except OidcError:
                raise
            except Exception as e:
                # refetch keys at most once a minute, so bad tokens cannot make us hammer the IdP
                if attempt == 0 and time.time() - self._jwks_at > 60:
                    continue
                raise OidcError(f"id_token rejected: {type(e).__name__}")
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("nonce mismatch")
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.s.oidc_client_id:
            raise OidcError("azp does not match client")
        if self.s.oidc_project_id:
            auds = aud if isinstance(aud, list) else [aud]
            if self.s.oidc_project_id not in auds:
                raise OidcError("token is not for the Desk Hub project")
        return dict(claims)

    def roles(self, claims: dict) -> set[str]:
        names = [self.s.oidc_role_claim] if self.s.oidc_role_claim else []
        if self.s.oidc_project_id:
            names.append(f"urn:zitadel:iam:org:project:{self.s.oidc_project_id}:roles")
        names.append("urn:zitadel:iam:org:project:roles")
        found: set[str] = set()
        for n in names:
            v = claims.get(n)
            if isinstance(v, dict):
                found |= set(v.keys())
        return found

    def logout_url(self) -> str | None:
        ep = self.endpoints().end_session
        if not ep:
            return None
        return f"{ep}?{urlencode({'client_id': self.s.oidc_client_id, 'post_logout_redirect_uri': self.s.oidc_post_logout_uri})}"


_client: OidcClient | None = None


def get_oidc() -> OidcClient:
    """FastAPI dependency; tests override it with a client wired to a fake identity provider."""
    global _client
    from ..config import get_settings
    if _client is None:
        _client = OidcClient(get_settings())
    return _client
