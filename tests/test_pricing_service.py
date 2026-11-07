import datetime as dt

from app import db as database
from app.models import Tenant, Wallet, now
from app.services import pricing
from .conftest import mk_tenant


def test_service_layer_can_price_from_a_trusted_provider_figure(client, cw):
    """Reserved for the future webhook receiver (which reads Meta's own figure). Not reachable from the internal API."""
    tid = mk_tenant(client, cw)
    with database.SessionLocal() as s:
        snap = pricing.price(s, s.get(Tenant, tid), s.get(Wallet, tid), "utility", "NG", now(), provider_cost="0.01", provider_currency="USD")
        assert snap.rate_source == "event" and snap.rate_card_id is None and snap.unit_cost_micro == 10_000 * 1600
