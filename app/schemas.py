from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Category = Literal["marketing", "utility", "authentication", "service"]


class TenantCreate(BaseModel):
    wisecp_service_id: str
    billing_currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")   # the customer's WiseCP billing currency
    name: str
    owner_email: EmailStr
    owner_name: str | None = None
    plan: str
    seats: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    ai_replies: int | None = Field(default=None, ge=0)
    markup: Decimal | None = Field(default=None, gt=0)
    kb_level: str | None = None
    extra_seats: int = Field(default=0, ge=0)
    extra_channels: int = Field(default=0, ge=0)
    extra_ai_packs: int = Field(default=0, ge=0)
    create_owner_user: bool = True
    chatwoot_account_id: int | None = None   # adopt an existing workspace (e.g. BennyBoom)
    chatwoot_user_id: int | None = None


class TenantPatch(BaseModel):
    name: str | None = None
    plan: str | None = None
    seats: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    ai_replies: int | None = Field(default=None, ge=0)
    markup: Decimal | None = Field(default=None, gt=0)
    kb_level: str | None = None
    extra_seats: int | None = Field(default=None, ge=0)
    extra_channels: int | None = Field(default=None, ge=0)
    extra_ai_packs: int | None = Field(default=None, ge=0)
    grant_seats: int | None = Field(default=None, ge=0)
    grant_channels: int | None = Field(default=None, ge=0)


class SuspendBody(BaseModel):
    mode: Literal["pause_outbound", "lock"] = "pause_outbound"
    reason: str | None = Field(default=None, max_length=300)


class CreditBody(BaseModel):
    amount: Decimal = Field(gt=0)                      # major units, e.g. "50000.00"
    currency: str = Field(min_length=3, max_length=3)   # must equal the wallet's currency
    idempotency_key: str = Field(min_length=3, max_length=150)
    reference: str | None = None


class CreditLimitBody(BaseModel):
    amount: Decimal = Field(ge=0)                      # major units; 0 switches credit off
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(min_length=3, max_length=300)


class RateBody(BaseModel):
    category: Category
    market: str = Field(default="*", min_length=1, max_length=2)       # ISO country or "*"
    currency: str = Field(min_length=3, max_length=3)                 # the currency Meta bills in
    rate: Decimal = Field(ge=0)                                       # per message, major units, e.g. "0.0067"
    effective_from: str                                               # YYYY-MM-DD
    effective_to: str | None = None
    source: str = Field(min_length=3, max_length=300)                 # where the figure came from (URL or file)
    source_ref: str | None = None


class FxBody(BaseModel):
    base: str = Field(default="USD", min_length=3, max_length=3)
    quote: str = Field(min_length=3, max_length=3)
    rate: Decimal = Field(gt=0)                                       # 1 base = rate quote
    effective_from: str | None = None                                 # ISO timestamp; default now
    source_ref: str | None = None


class SendCheck(BaseModel):
    tenant_id: str | None = None   # optional: derived from the credential when it covers one tenant
    category: Category
    country: str | None = None
    count: int = Field(default=1, ge=1)


class UsageEvent(BaseModel):
    # n8n and other callers report WHAT happened, never what it cost: unknown fields (e.g. a price) are rejected.
    model_config = ConfigDict(extra="forbid")
    tenant_id: str | None = None
    provider: Literal["meta_whatsapp"] = "meta_whatsapp"
    status: Literal["sent", "delivered", "read", "failed"] = "delivered"
    external_id: str = Field(min_length=1, max_length=160)
    send_id: str | None = Field(default=None, min_length=1, max_length=160)   # the reservation reference, if the hub held funds for this send
    category: Category
    country: str | None = None
    channel: str = "whatsapp"
    billed: bool = True
    occurred_at: str | None = None         # ISO date or datetime


class AiReply(BaseModel):
    tenant_id: str | None = None
    # A stable id for this ONE logical reply (e.g. the n8n execution id + the conversation message id). Retrying a failed
    # workflow with the same key counts once. Without a stable key, a retry could not be told apart from a new reply.
    key: str = Field(min_length=1, max_length=160)
    count: int = Field(default=1, ge=1, le=1000)


class Counts(BaseModel):
    seats_used: int | None = Field(default=None, ge=0)


ChannelType = Literal["whatsapp", "instagram", "messenger", "tiktok", "telegram", "email", "web_chat", "other"]


class ChannelCreate(BaseModel):
    channel_type: ChannelType
    provider: str = "chatwoot_native"
    display_name: str = Field(min_length=1, max_length=200)
    chatwoot_inbox_id: int | None = None
    phone_number: str | None = None
    external_id: str | None = None
    external_account_id: str | None = None
    status: Literal["pending", "connected", "disconnected", "error"] = "pending"
    verification_status: str | None = None
    config: dict | None = None      # non-secret settings only


class ChannelPatch(BaseModel):
    display_name: str | None = None
    status: Literal["pending", "connected", "disconnected", "error"] | None = None
    verification_status: str | None = None
    config: dict | None = None


class ChannelStatusReport(BaseModel):
    status: Literal["pending", "connected", "disconnected", "error"]
    verification_status: str | None = None


class RetryBody(BaseModel):
    chatwoot_account_id: int | None = None
    chatwoot_user_id: int | None = None
    confirm_not_created: bool = False


class PlanBody(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_-]+$")
    name: str
    price: Decimal | None = Field(default=None, ge=0)   # reporting only; WiseCP owns prices
    currency: str = "USD"
    seats: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    ai_replies: int | None = Field(default=None, ge=0)
    default_markup: Decimal | None = Field(default=None, gt=0)
    kb_level: str = "basic"


class PlanPatch(BaseModel):
    name: str | None = None
    active: bool | None = None
    price: Decimal | None = Field(default=None, ge=0)
    seats: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    ai_replies: int | None = Field(default=None, ge=0)
    default_markup: Decimal | None = Field(default=None, gt=0)
    kb_level: str | None = None


class MemberBody(BaseModel):
    email: EmailStr
    role: Literal["owner", "admin", "billing", "member"] = "member"


class CurrencyPatch(BaseModel):
    billing_enabled: bool


class ReserveBody(BaseModel):
    tenant_id: str | None = None
    send_id: str = Field(min_length=1, max_length=160)     # YOUR id for this send; repeating it returns the same hold
    category: Category
    country: str | None = None
    count: int = Field(default=1, ge=1, le=10000)


class AiAdjustBody(BaseModel):
    delta: int = Field(ge=-100000, le=100000)          # replies; negative gives allowance back, positive uses it
    reason: str = Field(min_length=3, max_length=300)
    idempotency_key: str = Field(min_length=3, max_length=150)
