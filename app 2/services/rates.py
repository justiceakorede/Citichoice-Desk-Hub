"""Versioned provider price lists (Meta rate cards)."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from .. import money
from ..models import Currency, RateCard

CATEGORIES = ("marketing", "utility", "authentication", "service")
PROVIDER = "meta_whatsapp"


class RateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def select_rate(db: Session, category: str, market: str | None, currency: str, on: dt.date,
                provider: str = PROVIDER) -> RateCard | None:
    """The rule: active rows for this provider, category and BILLING currency whose window [from, to) contains the
    date; a row for the exact market beats the '*' default. Overlaps are impossible, so at most one row per market."""
    m = (market or "*").upper()
    return db.scalar(
        select(RateCard).where(RateCard.provider == provider, RateCard.category == category, RateCard.currency == currency,
                               RateCard.active.is_(True), RateCard.market.in_([m, "*"]), RateCard.effective_from <= on,
                               or_(RateCard.effective_to.is_(None), RateCard.effective_to > on))
        .order_by(case((RateCard.market == m, 0), else_=1)).limit(1))


def add_rate(db: Session, *, category: str, market: str, currency: str, rate, effective_from: dt.date,
             source: str, actor: str, effective_to: dt.date | None = None, source_ref: str | None = None,
             provider: str = PROVIDER) -> RateCard:
    """Adds a new version. History is never edited: a new row must start after every existing one for the same key,
    and it closes an open-ended predecessor at its own start date. Backdating is refused."""
    category, market, currency = category.lower(), market.upper(), currency.upper()
    if category not in CATEGORIES:
        raise RateError("bad_category", f"category must be one of {CATEGORIES}")
    if market != "*" and not (len(market) == 2 and market.isalpha()):
        raise RateError("bad_market", "market is a two-letter country code or *")
    cur = db.get(Currency, currency)
    if not cur or cur.exponent is None:
        raise RateError("unknown_currency", f"{currency} cannot hold money")
    try:
        micro = money.major_to_micro(rate)
    except (money.MoneyError, TypeError) as e:
        raise RateError("bad_amount", str(e))
    if micro < 0:
        raise RateError("bad_amount", "rate cannot be negative")
    if effective_to is not None and effective_to <= effective_from:
        raise RateError("bad_window", "effective_to must be after effective_from")

    rows = db.scalars(select(RateCard).where(RateCard.provider == provider, RateCard.category == category,
                                             RateCard.market == market, RateCard.currency == currency,
                                             RateCard.active.is_(True)).order_by(RateCard.effective_from)).all()
    if any(r.effective_from >= effective_from for r in rows):
        raise RateError("overlap", "a rate card already starts on or after that date; history is not edited, add a later version")
    if rows:
        last = rows[-1]
        if last.effective_to is None:
            if effective_to is not None:
                raise RateError("overlap", "closing a window in the middle of an open-ended rate would leave a gap; give no end date")
            last.effective_to = effective_from            # the only change ever made to an existing row
        elif last.effective_to > effective_from:
            raise RateError("overlap", "the new window overlaps the previous one")
    version = (db.scalar(select(func.max(RateCard.version)).where(RateCard.provider == provider, RateCard.category == category,
                                                                  RateCard.market == market, RateCard.currency == currency)) or 0) + 1
    card = RateCard(provider=provider, category=category, market=market, currency=currency, rate_micro=micro,
                    effective_from=effective_from, effective_to=effective_to, source=source[:300], source_ref=source_ref,
                    version=version, active=True, created_by=actor)
    db.add(card)
    db.flush()
    return card
