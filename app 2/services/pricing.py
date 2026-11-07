"""Prices one message. Every figure that goes on a customer's account is derived here from named inputs, and the
inputs are frozen into an immutable PricingSnapshot, so any charge can be reproduced and no later change to a rate
card, an exchange rate or a markup can alter it."""
import datetime as dt
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import PricingSnapshot, Tenant, Wallet
from . import fx, rates

ROUNDING = "half_even_micro_v1"


class PricingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def compute(rate_micro: int, fx_rate: Decimal, buffer_pct: Decimal, markup: Decimal) -> tuple[int, int, int, int]:
    """Returns (unit_cost, buffer, fee, charge) in micro-units of the CUSTOMER's currency.
    unit_cost is the provider's actual price converted, with no buffer and no markup. Each step rounds once, half-even,
    to the nearest micro-unit; nothing is rounded to cents."""
    unit_cost = money.scale(rate_micro, fx_rate)
    buffer = money.scale(unit_cost, buffer_pct)
    charge = money.scale(unit_cost + buffer, markup)
    return unit_cost, buffer, charge - unit_cost - buffer, charge


def _hash(f: dict) -> str:
    return hashlib.sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_fields(*, provider, category, market, provider_currency, customer_currency, rate_source, rate_card_id,
                    rate_micro, fx_rate: Decimal, fx_source, fx_rate_ids, fx_observed_at, fx_buffer_pct: Decimal,
                    markup: Decimal) -> dict:
    unit, buf, fee, charge = compute(rate_micro, fx_rate, fx_buffer_pct, markup)
    return dict(provider=provider, category=category, market=market, provider_currency=provider_currency,
                customer_currency=customer_currency, rate_source=rate_source, rate_card_id=rate_card_id,
                rate_micro=rate_micro, fx_rate=fx_rate, fx_source=fx_source, fx_rate_ids=list(fx_rate_ids),
                fx_observed_at=fx_observed_at, fx_buffer_pct=fx_buffer_pct, markup=markup, rounding_rule=ROUNDING,
                unit_cost_micro=unit, buffer_micro=buf, fee_micro=fee, unit_charge_micro=charge)


def hash_of(f: dict) -> str:
    return _hash({k: (format(v, "f") if isinstance(v, Decimal) else v.isoformat() if isinstance(v, dt.datetime) else v)
                  for k, v in f.items()})


def price(db: Session, tenant: Tenant, wallet: Wallet, category: str, country: str | None, at: dt.datetime, *,
          provider_cost=None, provider_currency: str | None = None, persist: bool = False) -> PricingSnapshot:
    """Raises PricingError (never returns a zero price) when a rate or exchange rate is missing or stale.
    `provider_cost` lets a trusted event supply the provider's own figure instead of the rate card."""
    s = get_settings()
    at = at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)
    market = (country or "*").upper()
    pcur = (provider_currency or s.provider_currency).upper()
    if provider_cost is not None:
        try:
            rate_micro, card_id, rate_source = money.major_to_micro(provider_cost), None, "event"
        except (money.MoneyError, TypeError) as e:
            raise PricingError("bad_provider_cost", str(e))
    else:
        card = rates.select_rate(db, category, market, pcur, at.date())
        if card is None:
            raise PricingError("no_rate_card", f"no {pcur} rate card for {category}/{market} on {at.date()}")
        rate_micro, card_id, rate_source = card.rate_micro, card.id, "rate_card"
    try:
        fx_rate, fx_rows = fx.rate_between(db, pcur, wallet.currency, at)
    except fx.FxError as e:
        raise PricingError(e.code, str(e))
    f = snapshot_fields(provider=rates.PROVIDER, category=category, market=market, provider_currency=pcur,
                        customer_currency=wallet.currency, rate_source=rate_source, rate_card_id=card_id,
                        rate_micro=rate_micro, fx_rate=fx_rate, fx_source="identity" if not fx_rows else "+".join(sorted({r.source for r in fx_rows})),
                        fx_rate_ids=[r.id for r in fx_rows], fx_observed_at=min((r.effective_from for r in fx_rows), default=None),
                        fx_buffer_pct=Decimal(s.fx_buffer_pct), markup=Decimal(tenant.markup))
    h = hash_of(f)
    if not persist:
        return PricingSnapshot(snapshot_hash=h, **f)
    existing = db.scalar(select(PricingSnapshot).where(PricingSnapshot.snapshot_hash == h))
    if existing:
        return existing
    snap = PricingSnapshot(snapshot_hash=h, **f)
    try:
        with db.begin_nested():
            db.add(snap)
            db.flush()
    except IntegrityError:
        return db.scalar(select(PricingSnapshot).where(PricingSnapshot.snapshot_hash == h))
    return snap
