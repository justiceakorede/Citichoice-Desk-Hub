import datetime as dt
import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import ApiKey, ApiKeyTenant, now


def hash_key(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def new_key() -> str:
    return "chd_" + secrets.token_urlsafe(32)


def client_ip(request: Request) -> str:
    hops = get_settings().trusted_proxy_hops
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff and hops > 0 and len(xff) >= hops:
        return xff[-hops]
    return request.client.host if request.client else ""


def require_scope(scope: str):
    def dep(request: Request, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer key")
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(authorization[7:].strip())))
        if not key or key.revoked:
            raise HTTPException(401, "invalid key")
        exp = key.expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=dt.timezone.utc)
            if exp < now():
                raise HTTPException(401, "key expired")
        if scope not in (key.scopes or []):
            raise HTTPException(403, "key lacks scope")
        if key.allowed_ips and client_ip(request) not in key.allowed_ips:
            raise HTTPException(403, "address not allowed")
        key.last_used_at = now()
        db.commit()
        request.state.actor = key.name
        return key
    return dep


admin_key = require_scope("admin")
internal_key = require_scope("internal")


def bound_tenant_ids(db: Session, key: ApiKey) -> list[str]:
    return list(db.scalars(select(ApiKeyTenant.tenant_id).where(ApiKeyTenant.api_key_id == key.id)))


def resolve_tenant_id(db: Session, key: ApiKey, supplied: str | None = None) -> str:
    """The credential decides which tenant a request may touch. A supplied id can only select among
    the tenants the credential is bound to; it can never widen access. Unbound = no access."""
    ids = bound_tenant_ids(db, key)
    if supplied is not None:
        if supplied not in ids:
            raise HTTPException(404, {"code": "tenant_not_found", "message": "tenant not found"})
        return supplied
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise HTTPException(403, {"code": "no_tenant_binding", "message": "credential is not bound to any tenant"})
    raise HTTPException(422, {"code": "tenant_required", "message": "credential covers several tenants; pass tenant_id"})
