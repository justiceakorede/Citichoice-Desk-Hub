from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Plan, Tenant, TenantEntitlement

SEED_PLANS = [  # editable data, loaded by migration 0002. price_micro is for reporting only; WiseCP owns prices.
    dict(code="starter",  name="Starter",  price_micro=None, seats=2,  channels=1, ai_replies=2000,  default_markup="1.500", kb_level="basic"),
    dict(code="growth",   name="Growth",   price_micro=None, seats=5,  channels=2, ai_replies=6000,  default_markup="1.400", kb_level="custom"),
    dict(code="business", name="Business", price_micro=None, seats=10, channels=4, ai_replies=20000, default_markup="1.300", kb_level="custom"),
    dict(code="scale",    name="Scale",    price_micro=None, seats=None, channels=None, ai_replies=None, default_markup=None, kb_level="custom"),
]
OVERRIDE_KEYS = {"seats": "override_seats", "channels": "override_channels", "ai_replies": "override_ai_replies",
                 "markup": "override_markup", "kb_level": "override_kb_level"}
ADDON_KEYS = ("extra_seats", "extra_channels", "extra_ai_packs", "grant_seats", "grant_channels")


def err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def seed_plans(db: Session) -> None:
    for p in SEED_PLANS:
        if not db.scalar(select(Plan).where(Plan.code == p["code"])):
            db.add(Plan(**{**p, "default_markup": Decimal(p["default_markup"]) if p["default_markup"] else None}))
    db.flush()


def get_plan(db: Session, code: str, must_be_active: bool = True) -> Plan:
    plan = db.scalar(select(Plan).where(Plan.code == code))
    if not plan or (must_be_active and not plan.active):
        raise err(422, "unknown_plan", f"plan '{code}' does not exist or is not active")
    return plan


def snapshot(t: Tenant) -> dict:
    e = t.entitlement
    return dict(plan=t.plan, seats=t.seats_limit, channels=t.channels_limit, ai_replies=t.ai_limit,
                markup=str(t.markup) if t.markup is not None else None, kb_level=t.kb_level, version=e.version,
                overrides={k: (str(getattr(e, v)) if getattr(e, v) is not None else None) for k, v in OVERRIDE_KEYS.items()},
                addons={k: getattr(e, k) for k in ADDON_KEYS})


def ensure_complete(t: Tenant) -> None:
    missing = [n for n, v in (("seats", t.seats_limit), ("channels", t.channels_limit),
                              ("ai_replies", t.ai_limit if t.entitlement.override_ai_replies is not None
                               or t.entitlement.plan.ai_replies is not None else None),
                              ("markup", t.markup)) if v is None]
    if missing:
        raise err(422, "entitlement_incomplete",
                  f"plan '{t.plan}' is by agreement: set {', '.join(missing)} explicitly")


def create_entitlement(db: Session, t: Tenant, plan: Plan, data: dict) -> TenantEntitlement:
    e = TenantEntitlement(tenant_id=t.id, plan=plan, plan_id=plan.id)
    for k, col in OVERRIDE_KEYS.items():
        if data.get(k) is not None:
            setattr(e, col, data[k])
    for k in ADDON_KEYS:
        setattr(e, k, data.get(k) or 0)
    t.entitlement = e
    db.add(e)
    ensure_complete(t)
    return e


def apply_change(db: Session, t: Tenant, d: dict) -> tuple[dict, dict]:
    """Applies a PATCH. Returns (before, after) snapshots for the audit record."""
    before = snapshot(t)
    e = t.entitlement
    if d.get("plan") and d["plan"] != t.plan:
        e.plan = get_plan(db, d["plan"])
        e.plan_id = e.plan.id
        for col in OVERRIDE_KEYS.values():     # a plan change drops old overrides; new ones come from this request
            setattr(e, col, None)
    for k, col in OVERRIDE_KEYS.items():
        if d.get(k) is not None:
            setattr(e, col, d[k])
    for k in ADDON_KEYS:
        if d.get(k) is not None:
            setattr(e, k, d[k])
    if d.get("name"):
        t.name = d["name"]
    ensure_complete(t)
    e.version += 1
    return before, snapshot(t)
