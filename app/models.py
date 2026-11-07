import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (DDL, JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index,
                        Integer, Numeric, String, UniqueConstraint, text)
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import get_settings


def uid() -> str:
    return uuid.uuid4().hex


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


STATUSES = ("pending", "provisioning", "provisioning_failed", "active", "suspended",
            "cancelling", "cancelled", "terminated")
SUSPEND_MODES = ("none", "pause_outbound", "lock")
CHANNEL_TYPES = ("whatsapp", "instagram", "messenger", "tiktok", "telegram", "email", "web_chat", "other")
JOB_STATUSES = ("queued", "running", "succeeded", "blocked", "dead")


def _in(col: str, values) -> str:
    return f"{col} in (" + ", ".join(f"'{v}'" for v in values) + ")"


class Currency(Base):
    """ISO 4217 currencies. `exponent` is the official minor-unit digit count. A currency can hold wallets only
    once staff switch billing on; codes WiseCP knows that are not in the current ISO list are flagged."""
    __tablename__ = "currencies"
    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    numeric: Mapped[str | None] = mapped_column(String(3))
    name: Mapped[str] = mapped_column(String(100))
    exponent: Mapped[int | None] = mapped_column(Integer)          # NULL for funds and metals: cannot hold money
    iso_status: Mapped[str] = mapped_column(String(24), default="iso")
    billing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("exponent is null or (exponent >= 0 and exponent <= 4)", name="ck_currency_exponent"),
                      CheckConstraint("iso_status in ('iso', 'not_in_current_iso')", name="ck_currency_status"))


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    price_micro: Mapped[int | None] = mapped_column(BigInteger)        # reporting only; WiseCP bills
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), default="USD")
    seats: Mapped[int | None] = mapped_column(Integer)                 # NULL = by agreement (overrides required)
    channels: Mapped[int | None] = mapped_column(Integer)
    ai_replies: Mapped[int | None] = mapped_column(Integer)
    default_markup: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    kb_level: Mapped[str] = mapped_column(String(20), default="basic")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantEntitlement(Base):
    """What this tenant is entitled to: a plan plus explicit, audited overrides and add-ons."""
    __tablename__ = "tenant_entitlements"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    override_seats: Mapped[int | None] = mapped_column(Integer)
    override_channels: Mapped[int | None] = mapped_column(Integer)
    override_ai_replies: Mapped[int | None] = mapped_column(Integer)
    override_markup: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    override_kb_level: Mapped[str | None] = mapped_column(String(20))
    extra_seats: Mapped[int] = mapped_column(Integer, default=0)       # paid add-ons
    extra_channels: Mapped[int] = mapped_column(Integer, default=0)
    extra_ai_packs: Mapped[int] = mapped_column(Integer, default=0)
    grant_seats: Mapped[int] = mapped_column(Integer, default=0)       # free grants by staff
    grant_channels: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    plan: Mapped["Plan"] = relationship(lazy="joined")


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    wisecp_service_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_email: Mapped[str] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(String(24), default="pending")
    suspend_mode: Mapped[str] = mapped_column(String(20), default="none")
    suspension_reason: Mapped[str | None] = mapped_column(String(300))
    suspended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    billing_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))   # from WiseCP; the wallet follows it
    seats_used: Mapped[int] = mapped_column(Integer, default=0)
    chatwoot_account_id: Mapped[int | None] = mapped_column(Integer)
    chatwoot_user_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    entitlement: Mapped["TenantEntitlement"] = relationship(lazy="joined", uselist=False)

    __table_args__ = (CheckConstraint(_in("status", STATUSES), name="ck_tenants_status"),
                      CheckConstraint(_in("suspend_mode", SUSPEND_MODES), name="ck_tenants_suspend_mode"))

    def _eff(self, override, base, extra=0, grant=0):
        v = override if override is not None else base
        return None if v is None else v + extra + grant

    @property
    def plan(self) -> str: return self.entitlement.plan.code
    @property
    def kb_level(self) -> str:
        e = self.entitlement
        return e.override_kb_level or e.plan.kb_level
    @property
    def markup(self) -> Decimal:
        e = self.entitlement
        return e.override_markup if e.override_markup is not None else e.plan.default_markup
    @property
    def seats_limit(self) -> int:
        e = self.entitlement
        return self._eff(e.override_seats, e.plan.seats, e.extra_seats, e.grant_seats)
    @property
    def channels_limit(self) -> int:
        e = self.entitlement
        return self._eff(e.override_channels, e.plan.channels, e.extra_channels, e.grant_channels)
    @property
    def ai_limit(self) -> int:
        e = self.entitlement
        base = self._eff(e.override_ai_replies, e.plan.ai_replies)
        return base + e.extra_ai_packs * get_settings().ai_pack_size


class TenantChannel(Base):
    __tablename__ = "tenant_channels"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(40), default="chatwoot_native")
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(String(200))
    phone_number: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(100))          # e.g. WhatsApp phone_number_id, IG account id
    external_account_id: Mapped[str | None] = mapped_column(String(100))  # e.g. WABA id
    status: Mapped[str] = mapped_column(String(20), default="pending")   # pending|connected|disconnected|error|removed
    verification_status: Mapped[str | None] = mapped_column(String(30))
    config: Mapped[dict | None] = mapped_column(JSON)                     # non-secret only
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        CheckConstraint(_in("channel_type", CHANNEL_TYPES), name="ck_channels_type"),
        CheckConstraint(_in("status", ("pending", "connected", "disconnected", "error", "removed")), name="ck_channels_status"),
        # A given external number/account can belong to only one tenant. Removed channels free the identifier.
        Index("uq_channel_external", "channel_type", "external_id", unique=True,
              postgresql_where=text("external_id is not null and status <> 'removed'"),
              sqlite_where=text("external_id is not null and status <> 'removed'")),
        Index("uq_channel_inbox", "tenant_id", "chatwoot_inbox_id", unique=True,
              postgresql_where=text("chatwoot_inbox_id is not null and status <> 'removed'"),
              sqlite_where=text("chatwoot_inbox_id is not null and status <> 'removed'")),
    )


class ProvisioningJob(Base):
    __tablename__ = "provisioning_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    operation: Mapped[str] = mapped_column(String(40))     # provision_tenant | sync_limits | sync_chatwoot_status
    status: Mapped[str] = mapped_column(String(12), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)      # step markers and external ids, never secrets
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_by: Mapped[str | None] = mapped_column(String(80))
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
                      Index("ix_jobs_claim", "status", "next_attempt_at"))


class ApiKeyTenant(Base):
    """Which tenants an internal credential may act on. No rows = no access."""
    __tablename__ = "api_key_tenants"
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)


class TenantMember(Base):
    """Server-side membership: which SSO identities may see which tenant."""
    __tablename__ = "tenant_members"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    subject: Mapped[str | None] = mapped_column(String(200), index=True)   # IdP "sub"; set on first verified login
    email: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="member")        # owner | admin | billing | member
    status: Mapped[str] = mapped_column(String(12), default="active")      # active | invited | revoked
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)


class Wallet(Base):
    __tablename__ = "wallets"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)   # funds the tenant has paid in and not used. Never negative
    receivable_micro: Mapped[int] = mapped_column(BigInteger, default=0)  # what the tenant OWES us for charges the provider already made
    reserved_micro: Mapped[int] = mapped_column(BigInteger, default=0)    # held against sends in flight (hub-controlled paths only)
    credit_limit_micro: Mapped[int] = mapped_column(BigInteger, default=0)  # how much new sends may rest on credit. Default 0
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("tenant_id", "currency", name="uq_wallet_tenant_currency"),
                      CheckConstraint("balance_micro >= 0", name="ck_wallet_balance_nonneg"),
                      CheckConstraint("receivable_micro >= 0", name="ck_wallet_receivable_nonneg"),
                      CheckConstraint("reserved_micro >= 0", name="ck_wallet_reserved_nonneg"),
                      CheckConstraint("credit_limit_micro >= 0", name="ck_wallet_credit_limit_nonneg"),
                      # Funds and debt never coexist: a payment settles the debt first.
                      CheckConstraint("balance_micro = 0 or receivable_micro = 0", name="ck_wallet_no_funds_and_debt"))


class LedgerEntry(Base):
    """Append-only. The balance is never edited without a row here."""
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))        # credit | debit | adjustment | receivable_settlement | reclassification
    currency: Mapped[str] = mapped_column(String(3))
    amount_micro: Mapped[int] = mapped_column(BigInteger)  # signed
    balance_after_micro: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    reference: Mapped[str | None] = mapped_column(String(200))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    # the DB itself guarantees an entry is in its wallet's currency, and that it is a known kind of entry
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "currency"], ["wallets.tenant_id", "wallets.currency"],
                                           name="fk_ledger_wallet_currency"),
                      CheckConstraint("kind in ('credit', 'debit', 'adjustment', 'receivable_settlement', 'reclassification')",
                                      name="ck_ledger_kind"))


class ReceivableEntry(Base):
    """Append-only record of what a tenant owes for charges already made by the provider. Accruals are positive;
    payments and write-offs are negative. The wallet's `receivable_micro` always equals the sum of these."""
    __tablename__ = "receivable_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(20))                       # accrual | settlement | writeoff | adjustment
    amount_micro: Mapped[int] = mapped_column(BigInteger)               # signed
    balance_after_micro: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    reference: Mapped[str | None] = mapped_column(String(200))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "currency"], ["wallets.tenant_id", "wallets.currency"],
                                           name="fk_receivable_wallet_currency"),
                      CheckConstraint("kind in ('accrual', 'settlement', 'writeoff', 'adjustment')", name="ck_receivable_kind"))


ADJUSTMENT_KINDS = ("wallet_credit", "wallet_debit", "receivable_writeoff", "manual_charge", "usage_reversal")


class AdjustmentRequest(Base):
    """A manual change to money, proposed by one staff member and decided by another (maker-checker). Nothing here is
    edited after the decision; the ledgers carry the effect as new entries keyed `adj:<id>`."""
    __tablename__ = "adjustment_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(24))
    amount_micro: Mapped[int] = mapped_column(BigInteger)                      # always positive; the kind gives the direction
    target_usage_id: Mapped[str | None] = mapped_column(ForeignKey("usage_records.id"))
    reason: Mapped[str] = mapped_column(String(300))
    evidence_ref: Mapped[str] = mapped_column(String(200))                      # ticket, invoice or email reference
    status: Mapped[str] = mapped_column(String(10), default="pending")          # pending | posted | rejected
    requested_by: Mapped[str] = mapped_column(String(200))
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(300))
    self_approved: Mapped[bool] = mapped_column(Boolean, default=False)         # only ever true under the configured small-amount limit
    __table_args__ = (CheckConstraint("kind in ('wallet_credit', 'wallet_debit', 'receivable_writeoff', 'manual_charge', 'usage_reversal')", name="ck_adjustment_kind"),
                      CheckConstraint("status in ('pending', 'posted', 'rejected')", name="ck_adjustment_status"),
                      CheckConstraint("amount_micro > 0", name="ck_adjustment_amount"),
                      CheckConstraint("decided_by is null or decided_by <> requested_by or self_approved", name="ck_adjustment_two_people"),
                      Index("ix_adjustment_status", "status", "requested_at"))


class UsageReversal(Base):
    """A usage charge can be reversed once. The usage record itself is never edited."""
    __tablename__ = "usage_reversals"
    usage_id: Mapped[str] = mapped_column(ForeignKey("usage_records.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("adjustment_requests.id"), unique=True)
    amount_micro: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reservation(Base):
    """Funds held for one send the hub is controlling. Only 'held' reservations count against the wallet."""
    __tablename__ = "reservations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(160))                 # the caller's own id for this send; makes reserve idempotent
    amount_micro: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(10), default="held")      # held | settled | released | expired
    pricing_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("pricing_snapshots.id"))
    settled_micro: Mapped[int | None] = mapped_column(BigInteger)       # what was finally charged, when settled
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "reference", name="uq_reservation_reference"),
                      CheckConstraint("amount_micro >= 0", name="ck_reservation_amount"),
                      CheckConstraint("state in ('held', 'settled', 'released', 'expired')", name="ck_reservation_state"),
                      Index("ix_reservation_due", "state", "expires_at"))


class UsageRecord(Base):
    """One billable (or free) message outcome. Every money figure comes from the immutable pricing snapshot it points at,
    so later rate, FX or markup changes can never alter it."""
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30), default="meta_whatsapp")
    external_id: Mapped[str] = mapped_column(String(160))                # the provider's message id (a WhatsApp wamid)
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp")
    category: Mapped[str] = mapped_column(String(20))    # marketing|utility|authentication|service
    country: Mapped[str | None] = mapped_column(String(2))
    billed: Mapped[bool] = mapped_column(Boolean, default=True)
    customer_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    provider_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), default="USD")
    pricing_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("pricing_snapshots.id"))   # NULL only for unbilled messages
    provider_cost_micro: Mapped[int] = mapped_column(BigInteger, default=0)   # the provider's price, in the customer's currency, no buffer
    fx_buffer_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    service_fee_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    charge_micro: Mapped[int] = mapped_column(BigInteger, default=0)          # what the customer pays = cost + buffer + fee
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_usage_provider_event"),
                      Index("ix_usage_tenant_month", "tenant_id", "created_at"))


class MessageEvent(Base):
    """Every lifecycle event received for a message (sent, delivered, read, failed), kept apart from money. Only the
    configured billable status ever creates a charge, so four events for one message cannot be billed four times."""
    __tablename__ = "message_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(12))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("provider", "external_id", "status", name="uq_message_event"),
                      CheckConstraint("status in ('sent', 'delivered', 'read', 'failed')", name="ck_message_event_status"))


class RateCard(Base):
    """What the provider (Meta) charges per message, versioned. Amounts never change once written; a new version
    closes the previous one (`effective_to`). Selection: market-specific beats '*'; the window is [from, to)."""
    __tablename__ = "rate_cards"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(30), default="meta_whatsapp")
    category: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(2))                      # ISO country code, or "*" for the default
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))   # the currency Meta bills in
    rate_micro: Mapped[int] = mapped_column(BigInteger)                  # per message, micro-units of `currency`
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)           # exclusive; NULL = open-ended
    source: Mapped[str] = mapped_column(String(300), default="manual")   # URL or file the figure came from
    source_ref: Mapped[str | None] = mapped_column(String(64))           # e.g. sha256 of the imported file
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    __table_args__ = (UniqueConstraint("provider", "category", "market", "currency", "effective_from", name="uq_rate_card_start"),
                      CheckConstraint("rate_micro >= 0", name="ck_rate_nonneg"),
                      CheckConstraint("effective_to is null or effective_to > effective_from", name="ck_rate_window"))


class FxRate(Base):
    """Append-only exchange-rate observations: 1 `base_currency` = `rate` `quote_currency`. Never updated.
    A suspicious jump is stored as 'quarantined' and ignored until staff approve it (which adds a NEW active row)."""
    __tablename__ = "fx_rates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    base_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    quote_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    rate: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    source: Mapped[str] = mapped_column(String(60))                      # manual | wisecp:currencies | phase2_manual
    source_ref: Mapped[str | None] = mapped_column(String(200))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(12), default="active")    # active | quarantined
    approves_id: Mapped[str | None] = mapped_column(ForeignKey("fx_rates.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    __table_args__ = (UniqueConstraint("base_currency", "quote_currency", "source", "effective_from", name="uq_fx_observation"),
                      CheckConstraint("rate > 0", name="ck_fx_positive"),
                      CheckConstraint("status in ('active', 'quarantined')", name="ck_fx_status"),
                      Index("ix_fx_pair_time", "base_currency", "quote_currency", "effective_from"))


class PricingSnapshot(Base):
    """IMMUTABLE record of exactly how one message was priced. Rows are shared between messages priced identically
    (same hash). A database trigger refuses UPDATE and DELETE."""
    __tablename__ = "pricing_snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    snapshot_hash: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[str] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(2))
    provider_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    customer_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    rate_source: Mapped[str] = mapped_column(String(20))                 # rate_card | event | reconstructed
    rate_card_id: Mapped[str | None] = mapped_column(ForeignKey("rate_cards.id"))
    rate_micro: Mapped[int] = mapped_column(BigInteger)                  # provider price per message, provider currency
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(24, 10))            # provider currency -> customer currency (1 if equal)
    fx_source: Mapped[str] = mapped_column(String(60))
    fx_rate_ids: Mapped[list] = mapped_column(JSON, default=list)        # the FxRate rows used (two for a cross rate)
    fx_observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    fx_buffer_pct: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    markup: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    rounding_rule: Mapped[str] = mapped_column(String(30), default="half_even_micro_v1")
    unit_cost_micro: Mapped[int] = mapped_column(BigInteger)             # ACTUAL provider cost in the customer's currency
    buffer_micro: Mapped[int] = mapped_column(BigInteger)
    fee_micro: Mapped[int] = mapped_column(BigInteger)
    unit_charge_micro: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class AiUsage(Base):
    __tablename__ = "ai_usage"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)   # YYYY-MM
    replies: Mapped[int] = mapped_column(Integer, default=0)


class AiConsumption(Base):
    """One logical AI reply (or one staff adjustment) counted against the monthly allowance. The caller's own stable
    key makes a retried workflow count once. `ai_usage.replies` always equals the sum of the claimed units here."""
    __tablename__ = "ai_consumptions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))                      # YYYY-MM in Africa/Lagos, fixed when claimed
    consumption_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(12), default="reply")      # reply | adjustment
    units: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10), default="claimed")   # claimed | released
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "consumption_key", name="uq_ai_consumption_key"),
                      CheckConstraint("kind in ('reply', 'adjustment')", name="ck_ai_kind"),
                      CheckConstraint("state in ('claimed', 'released')", name="ck_ai_state"),
                      CheckConstraint("units <> 0", name="ck_ai_units"))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)       # ["admin"] | ["internal"]
    allowed_ips: Mapped[list] = mapped_column(JSON, default=list)  # empty = any
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    tenant_id: Mapped[str | None] = mapped_column(String(32), index=True)
    detail: Mapped[dict | None] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(12), default="success")
    request_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class StaffSession(Base):
    """Server-side staff session. The cookie holds a random token; only its hash is stored.
    No OIDC tokens are kept: the hub needs the identity and roles once, at sign-in."""
    __tablename__ = "staff_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)          # sha256 of the cookie token
    sub: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    roles: Mapped[list] = mapped_column(JSON, default=list)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---- append-only guarantees, enforced by the database itself --------------------------------------------------
# Pricing snapshots and FX observations can never be changed or removed; rate-card amounts and windows' starts are
# frozen (only the closing date and the active flag may move). Staff tooling and bugs alike hit the same wall.
_PG_FN = DDL("""
CREATE OR REPLACE FUNCTION desk_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '%% is append-only (%%)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END; $$ LANGUAGE plpgsql""")
_RC_COLS = ("rate_micro", "currency", "market", "category", "provider", "effective_from")
_PG_TRIGGERS = [
    DDL("CREATE OR REPLACE TRIGGER pricing_snapshots_immutable BEFORE UPDATE OR DELETE ON pricing_snapshots FOR EACH ROW EXECUTE FUNCTION desk_immutable()"),
    DDL("CREATE OR REPLACE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION desk_immutable()"),
    DDL("CREATE OR REPLACE TRIGGER receivable_entries_immutable BEFORE UPDATE OR DELETE ON receivable_entries FOR EACH ROW EXECUTE FUNCTION desk_immutable()"),
    DDL("CREATE OR REPLACE TRIGGER fx_rates_immutable BEFORE UPDATE OR DELETE ON fx_rates FOR EACH ROW EXECUTE FUNCTION desk_immutable()"),
    DDL("CREATE OR REPLACE TRIGGER rate_cards_frozen BEFORE UPDATE ON rate_cards FOR EACH ROW WHEN ("
        + " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in _RC_COLS) + ") EXECUTE FUNCTION desk_immutable()"),
    DDL("CREATE OR REPLACE TRIGGER rate_cards_no_delete BEFORE DELETE ON rate_cards FOR EACH ROW EXECUTE FUNCTION desk_immutable()"),
]
_SQLITE_TRIGGERS = [DDL(f"CREATE TRIGGER IF NOT EXISTS {t}_{op} BEFORE {op.upper()} ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
                    for t in ("pricing_snapshots", "fx_rates", "rate_cards", "ledger_entries", "receivable_entries")
                    for op in (("update", "delete") if t != "rate_cards" else ("delete",))]
_SQLITE_TRIGGERS.append(DDL("CREATE TRIGGER IF NOT EXISTS rate_cards_frozen BEFORE UPDATE ON rate_cards WHEN "
                            + " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _RC_COLS) + " BEGIN SELECT RAISE(ABORT, 'rate_cards amounts are frozen'); END"))
_PG_EXT = DDL("CREATE EXTENSION IF NOT EXISTS btree_gist")
_PG_NO_OVERLAP = DDL("""DO $$ BEGIN
  ALTER TABLE rate_cards ADD CONSTRAINT rate_cards_no_overlap EXCLUDE USING gist (
    provider WITH =, category WITH =, market WITH =, currency WITH =, daterange(effective_from, effective_to, '[)') WITH &&) WHERE (active);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$""")
event.listen(Base.metadata, "after_create", _PG_EXT.execute_if(dialect="postgresql"))
event.listen(Base.metadata, "after_create", _PG_NO_OVERLAP.execute_if(dialect="postgresql"))
event.listen(Base.metadata, "after_create", _PG_FN.execute_if(dialect="postgresql"))
for _d in _PG_TRIGGERS:
    event.listen(Base.metadata, "after_create", _d.execute_if(dialect="postgresql"))
for _d in _SQLITE_TRIGGERS:
    event.listen(Base.metadata, "after_create", _d.execute_if(dialect="sqlite"))
