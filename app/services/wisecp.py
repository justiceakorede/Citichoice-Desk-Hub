"""WiseCP invoices -> Desk wallet credit, exactly as docs/WISECP_AUDIT.md ("Top-up contract") decided.

Rules, all enforced here:
  * The hub READS the invoice from the WiseCP admin API. Nothing in a hook or request body is trusted for money or identity.
  * Only invoice lines for the Desk top-up add-on (id from WISECP_TOPUP_ADDON_ID, or the configured description as a
    fallback) that point at a service mapped to a hub tenant can credit. WiseCP's own balance top-ups never do.
  * Credit = the line's subtotal EXCLUDING tax, in the invoice currency, after any line discount. Tax never enters the wallet.
  * Only an invoice whose status is `paid` credits. Partial, unpaid, refunded: nothing.
  * One credit per line, ever: idempotency key `wisecp:invoice:{invoice}:line:{line}:credit`.
  * A wallet is never credited in a different currency than it holds.
  * A `paid` invoice with no payment rows, or payments short of the total by more than one minor unit (BankTransfer approvals),
    credits only up to TOPUP_REVIEW_THRESHOLD_USD; above that it WAITS FOR STAFF (nothing is credited, an audit row is written).

NOT built yet (see docs/IMPLEMENTATION_STATUS.md): reversal of a credit when WiseCP refunds or deletes the payment.

[ASSUMED] The exact JSON field names of GET /api/v1/admin/invoices/{id} are not verified against your install. All of the
parsing lives in `parse_invoice`, tolerant of alternatives and strict about failing closed. Run
`python -m app.cli wisecp-invoice 14` against your real invoice 14 before turning this on: it must show credit 18,657.52 NGN.
"""
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import Tenant, now
from . import fx
from . import tenants as ts
from . import wallet as wl


class WisecpError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _d(v, default=Decimal(0)) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else default
    except (InvalidOperation, ValueError):
        return default


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


@dataclass
class Line:
    line_id: str
    description: str
    quantity: Decimal
    unit: Decimal
    discount: Decimal
    discount_is_percent: bool
    service_id: str | None
    addon_id: str | None
    raw: dict = field(default_factory=dict)

    @property
    def subtotal(self) -> Decimal:
        """What was charged for the line before tax: quantity x unit, minus its discount."""
        gross = self.quantity * self.unit
        cut = gross * self.discount / Decimal(100) if self.discount_is_percent else self.discount
        return max(Decimal(0), gross - cut)


@dataclass
class Invoice:
    invoice_id: str
    status: str
    currency: str | None
    total: Decimal
    lines: list[Line] = field(default_factory=list)
    payments_in: Decimal = Decimal(0)
    payment_methods: list[str] = field(default_factory=list)
    payment_rows: int = 0


def parse_invoice(raw: dict) -> Invoice:
    d = raw.get("data", raw) if isinstance(raw, dict) else None
    if not isinstance(d, dict):
        raise WisecpError("bad_payload", "invoice payload is not an object")
    inv_id = _first(d, "id", "invoice_id")
    status = str(_first(d, "status", default="")).lower()
    cur = _first(d, "currency_code", "currency")
    cur = cur.upper() if isinstance(cur, str) and len(cur) == 3 and cur.isalpha() else None   # a numeric WiseCP id is NOT resolved: fail closed
    lines = []
    for it in d.get("items") or d.get("lines") or []:
        opts = it.get("options") if isinstance(it.get("options"), dict) else {}
        dtype = str(_first(it, "discount_type", default="amount")).lower()
        lines.append(Line(
            line_id=str(_first(it, "id", "item_id", default="")),
            description=str(_first(it, "description", "name", default="")),
            quantity=_d(_first(it, "quantity", "qty"), Decimal(1)), unit=_d(_first(it, "amount", "unit_amount", "price")),
            discount=_d(_first(it, "discount")), discount_is_percent=dtype in ("percent", "percentage", "rate", "%"),
            service_id=(str(_first(it, "user_pid", "service_id")) if _first(it, "user_pid", "service_id") else None),
            addon_id=(str(_first(it, "addon_id", default=_first(opts, "addon_id", "id", default=""))) or None), raw=it))
    pays = d.get("payments") or []
    paid_in = sum((_d(p.get("amount_in", p.get("amount"))) - _d(p.get("amount_out")) for p in pays), Decimal(0))
    return Invoice(invoice_id=str(inv_id), status=status, currency=cur, total=_d(_first(d, "total", "grand_total")), lines=lines,
                   payments_in=paid_in, payment_methods=[str(p.get("pmethod", p.get("payment_method", ""))) for p in pays], payment_rows=len(pays))


class WisecpClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.Client | None = None):
        self.base, self.key = base_url.rstrip("/"), api_key
        self.http = http or httpx.Client(timeout=15.0)

    def get_invoice(self, invoice_id: str) -> Invoice:
        if not (self.base and self.key):
            raise WisecpError("not_configured", "WISECP_API_URL / WISECP_API_KEY are not set")
        if not str(invoice_id).isdigit():
            raise WisecpError("bad_invoice_id")
        try:
            r = self.http.get(f"{self.base}/api/v1/admin/invoices/{invoice_id}", headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as e:
            raise WisecpError("unreachable", type(e).__name__) from e
        if r.status_code == 404:
            raise WisecpError("invoice_not_found")
        if r.status_code in (401, 403):
            raise WisecpError("insufficient_scope", "the WiseCP key cannot read invoices")
        if r.status_code >= 400:
            raise WisecpError(f"http_{r.status_code}")
        try:
            return parse_invoice(r.json())
        except ValueError as e:
            raise WisecpError("bad_payload") from e


def is_topup_line(line: Line) -> bool:
    s = get_settings()
    if line.addon_id is not None and line.addon_id == str(s.wisecp_topup_addon_id):
        return True
    # WiseCP often appends the service or plan name to an add-on's invoice line, so match the wording INSIDE the description.
    want = (s.wisecp_topup_description or "").strip().lower()
    return bool(want) and want in " ".join(line.description.lower().split())


def _anomaly(inv: Invoice, currency: str) -> str | None:
    """Paid, but WiseCP has no (or too little) payment record: bank-transfer approvals, hand-marked invoices."""
    if inv.payment_rows == 0:
        return "paid_without_payment_record"
    tol = Decimal(1).scaleb(-money.exponent_of(currency))          # one minor unit
    if inv.payments_in + tol < inv.total:
        return "payments_short_of_total"
    return None


def _usd_value(db: Session, amount: Decimal, currency: str) -> Decimal | None:
    try:
        rate, _ = fx.rate_between(db, "USD", currency, now())
    except fx.FxError:
        return None
    return amount / rate if rate else None


def process_invoice(db: Session, inv: Invoice, actor: str) -> dict:
    """Credit every qualifying line once. Returns one result per top-up line; non-top-up lines are ignored."""
    results = []
    if inv.status != "paid":
        return {"invoice_id": inv.invoice_id, "status": inv.status, "results": [], "note": "only paid invoices credit"}
    if inv.total <= 0:
        return {"invoice_id": inv.invoice_id, "status": inv.status, "results": [], "note": "zero-value invoice credits nothing"}
    for line in inv.lines:
        if not is_topup_line(line):
            continue
        res = {"line_id": line.line_id, "service_id": line.service_id}
        key = f"wisecp:invoice:{inv.invoice_id}:line:{line.line_id}:credit"
        t = db.scalar(select(Tenant).where(Tenant.wisecp_service_id == line.service_id)) if line.service_id else None
        if not line.line_id or t is None or t.status == "terminated":
            results.append({**res, "result": "skipped", "reason": "no_tenant_for_service"}); continue
        amount = line.subtotal
        if amount <= 0:
            results.append({**res, "result": "skipped", "reason": "zero_amount"}); continue
        if inv.currency is None:
            ts.audit(db, actor, "topup.needs_review", t.id, {"invoice": inv.invoice_id, "line": line.line_id, "reason": "currency_unresolved"}, outcome="held")
            results.append({**res, "result": "needs_review", "reason": "currency_unresolved"}); continue
        w = wl.get_wallet(db, t.id)
        if inv.currency != w.currency:
            ts.audit(db, actor, "topup.needs_review", t.id, {"invoice": inv.invoice_id, "line": line.line_id, "reason": "currency_mismatch",
                                                             "invoice_currency": inv.currency, "wallet_currency": w.currency}, outcome="held")
            results.append({**res, "result": "needs_review", "reason": "currency_mismatch"}); continue
        flags = {}
        if inv.payment_methods and all(m == "Balance" for m in inv.payment_methods):
            flags["funding_source"] = "wisecp_balance"
        why = _anomaly(inv, w.currency)
        if why:
            usd = _usd_value(db, amount, w.currency)
            limit = get_settings().topup_review_threshold_usd
            if usd is None or usd > limit:
                ts.audit(db, actor, "topup.needs_review", t.id, {"invoice": inv.invoice_id, "line": line.line_id, "reason": why,
                                                                 "amount": str(amount), "currency": w.currency}, outcome="held")
                results.append({**res, "result": "needs_review", "reason": why}); continue
            flags["flag"] = why
        # Never credit more than was charged: cut anything finer than the currency's minor unit (a % discount can produce it).
        amount = amount.quantize(Decimal(1).scaleb(-money.exponent_of(w.currency)), rounding=ROUND_DOWN)
        if amount <= 0:
            results.append({**res, "result": "skipped", "reason": "zero_amount"}); continue
        micro = money.input_to_micro(amount, w.currency)
        p = wl.credit(db, t.id, micro, key, kind="topup", reference=f"wisecp invoice {inv.invoice_id} line {line.line_id}", currency=w.currency,
                      meta={"source": "wisecp", "invoice_id": inv.invoice_id, "line_id": line.line_id, "tax_excluded": True, **flags})
        if p.created:
            ts.audit(db, actor, "topup.credit", t.id, {"invoice": inv.invoice_id, "line": line.line_id, "amount_micro": micro, **flags})
        results.append({**res, "result": "credited" if p.created else "already_credited", "amount_micro": micro, "currency": w.currency,
                        "balance_micro": p.balance_micro, **flags})
    db.commit()
    return {"invoice_id": inv.invoice_id, "status": inv.status, "results": results,
            "lines": [{"line_id": l.line_id, "description": l.description[:80], "addon_id": l.addon_id, "service_id": l.service_id} for l in inv.lines]}


def get_wisecp() -> WisecpClient:
    s = get_settings()
    return WisecpClient(s.wisecp_api_url, s.wisecp_api_key)
