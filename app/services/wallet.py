"""The wallet and its two ledgers. Amounts are micro-units (app/money.py).

Rules this module enforces, each also backed by a database constraint or trigger:
  * balance (funds paid in and unused) is never negative;
  * what the tenant OWES for charges the provider already made is a separate receivable, never a negative balance;
  * funds and debt never coexist: a payment clears the debt first;
  * every movement is an append-only ledger row with an idempotency key; a repeat changes nothing;
  * every money operation takes the wallet's row lock first, so concurrent requests cannot spend the same money twice.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import LedgerEntry, ReceivableEntry, Wallet


class InsufficientFunds(Exception):
    pass


class CurrencyMismatch(Exception):
    pass


@dataclass
class Posting:
    created: bool                 # False when this exact request had already been applied
    ledger_id: str | None
    balance_micro: int
    receivable_micro: int
    paid_micro: int = 0           # charge: taken from funds
    accrued_micro: int = 0        # charge: added to what the tenant owes
    settled_micro: int = 0        # credit: used to clear debt first


def lock_wallet(db: Session, tenant_id: str) -> Wallet:
    """Row-locks the wallet (PostgreSQL FOR UPDATE) and re-reads it, so the numbers we decide on are current."""
    w = db.execute(select(Wallet).where(Wallet.tenant_id == tenant_id).with_for_update()
                   .execution_options(populate_existing=True)).scalar_one_or_none()
    if w is None:
        raise LookupError("wallet not found")
    return w


def get_wallet(db: Session, tenant_id: str) -> Wallet:
    w = db.get(Wallet, tenant_id)
    if not w:
        raise LookupError("wallet not found")
    return w


def capacity_micro(w: Wallet) -> int:
    """How much NEW controlled spending the tenant may still start: funds, minus debt and holds, plus granted credit."""
    return w.balance_micro - w.receivable_micro - w.reserved_micro + w.credit_limit_micro


def _check_currency(w: Wallet, currency: str | None) -> None:
    if currency is not None and currency != w.currency:
        raise CurrencyMismatch(f"wallet is {w.currency}, amount is {currency}")


def _ledger(db, w, kind, amount, balance_after, key, reference, meta) -> LedgerEntry:
    e = LedgerEntry(tenant_id=w.tenant_id, kind=kind, amount_micro=amount, currency=w.currency,
                    balance_after_micro=balance_after, idempotency_key=key, reference=reference, meta=meta)
    db.add(e)
    return e


def _receivable(db, w, kind, amount, balance_after, key, reference, meta) -> ReceivableEntry:
    e = ReceivableEntry(tenant_id=w.tenant_id, currency=w.currency, kind=kind, amount_micro=amount,
                        balance_after_micro=balance_after, idempotency_key=key, reference=reference, meta=meta)
    db.add(e)
    return e


def _replay(db: Session, key: str, w: Wallet) -> Posting | None:
    le = db.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == key))
    re = db.scalar(select(ReceivableEntry).where(ReceivableEntry.idempotency_key == key))
    if le is None and re is None:
        return None
    settle = db.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == f"{key}:settle"))
    return Posting(created=False, ledger_id=le.id if le else None, balance_micro=w.balance_micro,
                   receivable_micro=w.receivable_micro,
                   paid_micro=-le.amount_micro if le is not None and le.amount_micro < 0 else 0,
                   accrued_micro=re.amount_micro if re is not None and re.amount_micro > 0 else 0,
                   settled_micro=-settle.amount_micro if settle is not None else 0)


def credit(db: Session, tenant_id: str, amount_micro: int, key: str, *, kind: str = "credit", reference: str | None = None,
           meta: dict | None = None, currency: str | None = None) -> Posting:
    """Money in. Any debt is cleared first; only the rest becomes usable funds."""
    if amount_micro <= 0:
        raise ValueError("credit must be positive")
    w = lock_wallet(db, tenant_id)
    _check_currency(w, currency)
    again = _replay(db, key, w)
    if again:
        return again
    settle = min(amount_micro, w.receivable_micro)
    try:
        with db.begin_nested():
            entry = _ledger(db, w, kind, amount_micro, w.balance_micro + amount_micro, key, reference, meta)
            if settle:
                _ledger(db, w, "receivable_settlement", -settle, w.balance_micro + amount_micro - settle, f"{key}:settle",
                        "payment cleared debt", meta)
                _receivable(db, w, "settlement", -settle, w.receivable_micro - settle, key, reference, meta)
            w.balance_micro += amount_micro - settle
            w.receivable_micro -= settle
            db.flush()
    except IntegrityError:
        again = _replay(db, key, lock_wallet(db, tenant_id))
        if again:
            return again
        raise
    return Posting(True, entry.id, w.balance_micro, w.receivable_micro, settled_micro=settle)


def debit(db: Session, tenant_id: str, amount_micro: int, key: str, *, kind: str = "debit", reference: str | None = None,
          meta: dict | None = None, currency: str | None = None) -> Posting:
    """Money out that must come from funds (staff adjustments, payouts). Never creates debt; never touches held funds."""
    if amount_micro <= 0:
        raise ValueError("debit must be positive")
    w = lock_wallet(db, tenant_id)
    _check_currency(w, currency)
    again = _replay(db, key, w)
    if again:
        return again
    if amount_micro > w.balance_micro - w.reserved_micro:
        raise InsufficientFunds()
    try:
        with db.begin_nested():
            entry = _ledger(db, w, kind, -amount_micro, w.balance_micro - amount_micro, key, reference, meta)
            w.balance_micro -= amount_micro
            db.flush()
    except IntegrityError:
        again = _replay(db, key, lock_wallet(db, tenant_id))
        if again:
            return again
        raise
    return Posting(True, entry.id, w.balance_micro, w.receivable_micro, paid_micro=amount_micro)


def charge(db: Session, tenant_id: str, amount_micro: int, key: str, *, reference: str | None = None, meta: dict | None = None,
           currency: str | None = None, release_hold_micro: int = 0) -> Posting:
    """A charge the PROVIDER has already made. It is always recorded and never refused: funds pay what they can and
    the rest becomes a receivable. This is NOT permission to start new sends; new controlled sends are gated by
    `capacity_micro` through reservations."""
    if amount_micro < 0:
        raise ValueError("charge cannot be negative")
    w = lock_wallet(db, tenant_id)
    _check_currency(w, currency)
    again = _replay(db, key, w)
    if again:
        return again
    pay = min(w.balance_micro, amount_micro)
    owe = amount_micro - pay
    try:
        with db.begin_nested():
            entry = _ledger(db, w, "debit", -pay, w.balance_micro - pay, key, reference, meta) if pay else None
            if owe:
                _receivable(db, w, "accrual", owe, w.receivable_micro + owe, key, reference, meta)
            w.balance_micro -= pay
            w.receivable_micro += owe
            w.reserved_micro = max(0, w.reserved_micro - release_hold_micro)
            db.flush()
    except IntegrityError:
        again = _replay(db, key, lock_wallet(db, tenant_id))
        if again:
            return again
        raise
    return Posting(True, entry.id if entry else None, w.balance_micro, w.receivable_micro, paid_micro=pay, accrued_micro=owe)


class NothingToWriteOff(Exception):
    pass


def writeoff(db: Session, tenant_id: str, amount_micro: int, key: str, *, reference: str | None = None,
             meta: dict | None = None, currency: str | None = None) -> Posting:
    """Forgives part of what the tenant owes. No funds move; the debt falls by an explicit, append-only entry."""
    if amount_micro <= 0:
        raise ValueError("write-off must be positive")
    w = lock_wallet(db, tenant_id)
    _check_currency(w, currency)
    again = _replay(db, key, w)
    if again:
        return again
    if amount_micro > w.receivable_micro:
        raise NothingToWriteOff()
    try:
        with db.begin_nested():
            _receivable(db, w, "writeoff", -amount_micro, w.receivable_micro - amount_micro, key, reference, meta)
            w.receivable_micro -= amount_micro
            db.flush()
    except IntegrityError:
        again = _replay(db, key, lock_wallet(db, tenant_id))
        if again:
            return again
        raise
    return Posting(True, None, w.balance_micro, w.receivable_micro, settled_micro=amount_micro)


def apply(db: Session, tenant_id: str, amount_micro: int, kind: str, idempotency_key: str, reference: str | None = None,
          meta: dict | None = None, currency: str | None = None) -> tuple[LedgerEntry, bool]:
    """Compatibility wrapper: positive amounts are credits (clearing debt first), negative amounts are funded debits."""
    p = (credit(db, tenant_id, amount_micro, idempotency_key, kind=kind, reference=reference, meta=meta, currency=currency)
         if amount_micro > 0 else
         debit(db, tenant_id, -amount_micro, idempotency_key, kind=kind, reference=reference, meta=meta, currency=currency))
    return db.get(LedgerEntry, p.ledger_id), p.created
