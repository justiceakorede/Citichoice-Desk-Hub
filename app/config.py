from decimal import Decimal
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"             # development | test | production
    database_url: str = ""
    secret_key: str = ""
    public_url: str = "https://hub.citichoice.net"

    chatwoot_url: str = ""               # e.g. https://desk.citichoice.net
    chatwoot_platform_token: str = ""    # Chatwoot super admin > Platform Apps token

    # Staff sign-in (Zitadel OIDC). The Basic-auth password is gone.
    oidc_issuer: str = ""                # exactly as in discovery, e.g. https://accounts.citichoice.co
    oidc_client_id: str = ""
    oidc_client_secret: str = ""         # set in the deployment platform only, never in git or chat
    oidc_redirect_uri: str = ""          # https://deskhub.citichoice.net/auth/callback
    oidc_post_logout_uri: str = ""       # https://deskhub.citichoice.net/auth/logged-out
    oidc_project_id: str = ""            # Zitadel project that holds the role
    oidc_required_role: str = "administrator"
    oidc_role_claim: str = ""            # default: urn:zitadel:iam:org:project:{project_id}:roles (plus the generic one)
    staff_ui_host: str = ""              # deskhub.citichoice.net: serves /ui and /auth only
    api_host: str = ""                   # hub.citichoice.net: serves /admin, /internal, /healthz only
    session_idle_minutes: int = 30
    session_absolute_hours: int = 8
    customer_api_enabled: bool = False   # customer routes stay off until a customer dashboard is approved
    wisecp_topup_url: str = ""           # where the "Top up" button sends the customer

    # Customer stats (Chatwoot reports). A dedicated "hub-reporter" Chatwoot user, added as an administrator of every tenant
    # account, supplies this token. Platform tokens cannot read account reports.
    chatwoot_report_token: str = ""
    service_messages_free: bool = False   # Meta now charges service messages (1,000 free per number per month); False = charge them like the rest
    price_display_market: str = "NG"    # market whose per-message prices the customer Overview shows (falls back to the default market *)
    stats_cache_seconds: int = 120       # Chatwoot report results are cached this long per tenant and range
    stats_http_timeout: float = 8.0

    # WhatsApp credit bought in WiseCP (docs/WISECP_AUDIT.md, "Top-up contract")
    wisecp_topup_addon_id: str = "48"                              # the add-on that sells Desk wallet credit
    wisecp_topup_description: str = "WhatsApp Prepaid Wallet Top-Up"   # fallback match on the invoice line text
    topup_review_threshold_usd: Decimal = Decimal("0")             # anomalous paid invoices above this wait for staff; 0 = all wait

    fx_buffer_pct: Decimal = Decimal("0.03")   # disclosed currency buffer
    # The delivery status that creates a charge. Meta bills per DELIVERED message, but the exact payload your Chatwoot/Meta
    # integration sends is not yet verified, so this is a setting, not a constant.
    billable_status: str = "delivered"
    # A manual money change normally needs a SECOND person to approve. A person may approve their own request only up to this
    # amount (in USD, converted at the current rate). 0 = never. Every self-approval is flagged and audited.
    adjustment_self_approval_limit_usd: Decimal = Decimal("0")
    reservation_ttl_hours: int = 72      # a hold with no outcome by then is released and flagged for reconciliation
    provider_currency: str = "USD"       # the currency Meta bills Citichoice in (per-WABA setting arrives with the channel work)
    fx_max_age_hours: int = 48           # an FX rate older than this (at the time of the message) is refused, not used
    fx_quarantine_pct: Decimal = Decimal("25")   # a move bigger than this is held for staff approval
    wisecp_api_url: str = ""             # e.g. https://billing.citichoice.co (no trailing slash)
    wisecp_api_key: str = ""             # a READ-ONLY key limited to the finance scope; set only in the deployment platform
    wisecp_base_currency: str = "USD"    # WiseCP's base currency; the hub refuses rates if WiseCP's base ever changes
    marketing_min_balance_usd: Decimal = Decimal("5")   # minimum wallet balance for marketing sends, converted to each wallet currency
    ai_pack_size: int = 5000
    suspend_grace_days: int = 7
    trusted_proxy_hops: int = 1          # Traefik (Coolify) = 1; Cloudflare + Traefik = 2


    def validate_for_runtime(self) -> None:
        """Refuse to boot production with weak or missing configuration."""
        if self.env != "production":
            return
        problems = []
        if not self.database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
            problems.append("DATABASE_URL must be PostgreSQL in production")
        if len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        for name in ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_redirect_uri", "oidc_project_id",
                     "staff_ui_host", "api_host"):
            if not getattr(self, name):
                problems.append(f"{name.upper()} is required in production")
        if self.oidc_issuer and not self.oidc_issuer.startswith("https://"):
            problems.append("OIDC_ISSUER must be https")
        if self.oidc_redirect_uri and self.staff_ui_host and not self.oidc_redirect_uri.startswith(f"https://{self.staff_ui_host}/"):
            problems.append("OIDC_REDIRECT_URI must be on STAFF_UI_HOST over https")
        if self.staff_ui_host and self.staff_ui_host == self.api_host:
            problems.append("STAFF_UI_HOST and API_HOST must differ")
        if not self.chatwoot_url or not self.chatwoot_platform_token:
            problems.append("CHATWOOT_URL and CHATWOOT_PLATFORM_TOKEN are required")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.env == "production":
            raise RuntimeError("DATABASE_URL is required")
        return "sqlite:///./dev.db"   # development convenience only, never production


@lru_cache
def get_settings() -> Settings:
    return Settings()
