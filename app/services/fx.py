"""Exchange rates: append-only observations, strict selection, a quarantine for suspicious moves, and a provider
boundary so the source of rates (WiseCP today) can change without touching pricing."""
import datetime as dt
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Currency, FxRate, now

TEN_DP = Decimal("0.0000000001")


class FxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Quote:
    base: str
    quote: str
    rate: Decimal                 # 1 base = rate quote
    source: str
    fetched_at: dt.datetime
    source_ref: str | None = None


def _aware(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d


def _latest(db: Session, base: str, quote: str, at: dt.datetime) -> FxRate | None:
    return db.scalar(select(FxRate).where(FxRate.base_currency == base, FxRate.quote_currency == quote,
                                          FxRate.status == "active", FxRate.effective_from <= at)
                     .order_by(FxRate.effective_from.desc()).limit(1))


def record_quotes(db: Session, quotes: list[Quote], actor: str, quarantine_pct: Decimal | None = None) -> dict:
    """Stores observations. Returns counts. Unchanged rates are not re-stored more than once a day."""
    pct = get_settings().fx_quarantine_pct if quarantine_pct is None else quarantine_pct
    out = {"stored": 0, "unchanged": 0, "quarantined": 0, "skipped": 0}
    for q in quotes:
        if q.rate <= 0 or q.base == q.quote or not db.get(Currency, q.base) or not db.get(Currency, q.quote):
            out["skipped"] += 1
            continue
        at = _aware(q.fetched_at)
        prev = _latest(db, q.base, q.quote, at)
        status = "active"
        if prev is not None:
            if prev.rate == q.rate and at - _aware(prev.effective_from) < dt.timedelta(hours=24):
                out["unchanged"] += 1
                continue
            with localcontext() as c:
                c.prec = 40
                move = abs(q.rate / prev.rate - 1) * 100
            if move > pct:
                status = "quarantined"
        row = FxRate(base_currency=q.base, quote_currency=q.quote, rate=q.rate, source=q.source, source_ref=q.source_ref,
                     fetched_at=at, effective_from=at, status=status, created_by=actor)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            out["unchanged"] += 1
            continue
        out["quarantined" if status == "quarantined" else "stored"] += 1
    return out


def approve(db: Session, fx_id: str, actor: str) -> FxRate:
    """Staff accept a quarantined rate. History is not edited: a NEW active row is added that points at the old one."""
    q = db.get(FxRate, fx_id)
    if not q or q.status != "quarantined":
        raise FxError("not_quarantined", "no such quarantined rate")
    if db.scalar(select(FxRate).where(FxRate.approves_id == q.id)):
        raise FxError("already_approved", "already approved")
    n = now()
    row = FxRate(base_currency=q.base_currency, quote_currency=q.quote_currency, rate=q.rate, source=q.source,
                 source_ref=f"approved:{q.id}", fetched_at=q.fetched_at, effective_from=max(n, _aware(q.effective_from)),
                 status="active", approves_id=q.id, created_by=actor)
    db.add(row)
    db.flush()
    return row


def _leg(db: Session, frm: str, to: str, at: dt.datetime) -> tuple[Decimal, FxRate] | None:
    d = _latest(db, frm, to, at)
    if d:
        return d.rate, d
    inv = _latest(db, to, frm, at)
    if inv:
        with localcontext() as c:
            c.prec = 40
            return (Decimal(1) / inv.rate).quantize(TEN_DP, rounding=ROUND_HALF_EVEN), inv
    return None


def rate_between(db: Session, frm: str, to: str, at: dt.datetime, hub_pivot: str = "USD") -> tuple[Decimal, list[FxRate]]:
    """1 `frm` in `to`, as at `at`. Direct, inverse, or crossed through the pivot. Refuses stale or missing rates."""
    if frm == to:
        return Decimal(1), []
    at = _aware(at)
    legs = []
    one = _leg(db, frm, to, at)
    if one:
        legs = [one]
    elif frm != hub_pivot and to != hub_pivot:
        a, b = _leg(db, frm, hub_pivot, at), _leg(db, hub_pivot, to, at)
        if a and b:
            legs = [a, b]
    if not legs:
        raise FxError("no_fx_rate", f"no exchange rate for {frm}->{to}")
    oldest = min(_aware(r.effective_from) for _, r in legs)
    if at - oldest > dt.timedelta(hours=get_settings().fx_max_age_hours):
        raise FxError("fx_stale", f"the newest {frm}->{to} rate is older than {get_settings().fx_max_age_hours}h")
    rate = Decimal(1)
    with localcontext() as c:
        c.prec = 40
        for r, _ in legs:
            rate *= r
        rate = rate.quantize(TEN_DP, rounding=ROUND_HALF_EVEN)
    return rate, [r for _, r in legs]


# ----------------------------------------------------------------------------------------------------- providers
class WiseCpCurrencies:
    """Reads WiseCP's own rates (it already runs exchangerate-api.com). Verified against dev.wisecp.com:
    GET /api/v1/admin/financial/currencies -> data[]{code, local, rate, ...} and
    GET /api/v1/admin/financial/currency-settings -> data{auto_rate_enabled, last_run_at, ...}.
    NOT yet run against your live WiseCP. Fails closed on anything unexpected."""
    name = "wisecp:currencies"

    def __init__(self, base_url: str, api_key: str, expected_base: str, http: httpx.Client | None = None):
        self.url, self.key, self.base = base_url.rstrip("/"), api_key, expected_base
        self.http = http or httpx.Client(timeout=15.0)

    def _get(self, path: str):
        try:
            r = self.http.get(f"{self.url}/api/v1/admin/financial/{path}", headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as e:
            raise FxError("provider_unreachable", f"WiseCP unreachable: {type(e).__name__}")
        if r.status_code != 200:
            raise FxError("provider_refused", f"WiseCP answered {r.status_code} for {path}")
        try:
            return json.loads(r.text, parse_float=Decimal)["data"]       # floats become exact Decimals, never binary floats
        except Exception:
            raise FxError("provider_bad_payload", f"unreadable response for {path}")

    def fetch(self) -> list[Quote]:
        settings = self._get("currency-settings")
        # WiseCP can stop refreshing without any error; its own last-run time is the only warning.
        try:
            last = _aware(dt.datetime.fromisoformat(str(settings["last_run_at"]).replace("Z", "+00:00")))
        except Exception:
            raise FxError("provider_stale", "WiseCP did not report a usable last_run_at; refusing its rates")
        if now() - last > dt.timedelta(hours=get_settings().fx_max_age_hours):
            raise FxError("provider_stale", f"WiseCP last refreshed its rates at {last.isoformat()}")
        data = self._get("currencies")
        if not isinstance(data, list):
            raise FxError("provider_bad_payload", "currencies is not a list")
        bases = [c for c in data if c.get("local")]
        if len(bases) != 1 or bases[0].get("code") != self.base:
            raise FxError("base_changed", f"WiseCP base currency is {[c.get('code') for c in bases]}, expected {self.base}")
        out = []
        for c in data:
            rate = c.get("rate")
            if c.get("local") or not isinstance(rate, Decimal) or rate <= 0 or not c.get("code"):
                continue
            out.append(Quote(base=self.base, quote=c["code"].upper(), rate=rate, source=self.name,
                             source_ref=f"currency_id={c.get('id')};wisecp_last_run={last.isoformat()}", fetched_at=now()))
        if not out:
            raise FxError("provider_bad_payload", "no usable rates in the response")
        return out


def get_fx_provider() -> WiseCpCurrencies:
    s = get_settings()
    if not (s.wisecp_api_url and s.wisecp_api_key):
        raise FxError("provider_not_configured", "WISECP_API_URL and WISECP_API_KEY are not set")
    return WiseCpCurrencies(s.wisecp_api_url, s.wisecp_api_key, s.wisecp_base_currency)
