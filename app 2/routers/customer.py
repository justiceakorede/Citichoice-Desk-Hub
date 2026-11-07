"""Customer-facing API. Tenant access comes only from the authenticated identity and server-side membership."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.identity import Identity, get_identity
from ..db import get_db
from ..models import Tenant, TenantMember
from ..services import tenants as ts

from .ui import customer_api_only

router = APIRouter(prefix="/customer", dependencies=[Depends(customer_api_only)])


def memberships(db: Session, who: Identity) -> list[TenantMember]:
    rows = list(db.scalars(select(TenantMember).where(TenantMember.subject == who.subject, TenantMember.status == "active")))
    if not rows and who.email and who.email_verified:
        # First sign-in: link an invitation to this subject, but only for a verified email.
        pending = list(db.scalars(select(TenantMember).where(
            TenantMember.email == who.email.lower(), TenantMember.subject.is_(None), TenantMember.status == "active")))
        for m in pending:
            m.subject = who.subject
        if pending:
            db.commit()
        rows = pending
    return rows


@router.get("/tenants")
def my_tenants(who: Identity = Depends(get_identity), db: Session = Depends(get_db)):
    out = []
    for m in memberships(db, who):
        t = db.get(Tenant, m.tenant_id)
        if t and t.status != "terminated":
            out.append({**ts.customer_view(db, t), "my_role": m.role})
    return out


@router.get("/tenants/{tid}")
def my_tenant(tid: str, who: Identity = Depends(get_identity), db: Session = Depends(get_db)):
    m = next((m for m in memberships(db, who) if m.tenant_id == tid), None)
    t = db.get(Tenant, tid) if m else None
    if not t or t.status == "terminated":      # same answer for "not yours" and "does not exist"
        raise HTTPException(404, {"code": "tenant_not_found", "message": "tenant not found"})
    return {**ts.customer_view(db, t), "my_role": m.role}
