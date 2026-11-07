"""Customer stats and WiseCP top-ups. NOT YET RUN: the build sandbox had no Python dependencies. Run `python -m pytest tests/test_stats_topups.py`."""
import pytest
from decimal import Decimal

from app.main import app
from app.routers.admin import get_reports, get_wisecp
from app.services import stats as stats_svc
from app.services.chatwoot_reports import ReportError
from app.services.wisecp import Invoice, Line, WisecpError, parse_invoice
from .conftest import ADMIN, H, mk_tenant


class FakeReports:
    def __init__(self):
        self.calls, self.fail_inbox = [], set()

    configured = True
    found = []

    def inboxes(self, account_id):
        return self.found

    def summary(self, account_id, since, until, inbox_id=None):
        self.calls.append((account_id, inbox_id))
        if inbox_id in self.fail_inbox:
            raise ReportError("http_500")
        if inbox_id:
            return {"conversations": 3, "incoming_messages": 10, "outgoing_messages": 5, "messages": 15, "resolutions": 1}
        return {"conversations": 40, "incoming_messages": 200, "outgoing_messages": 150, "messages": 350, "resolutions": 20}


@pytest.fixture()
def reports():
    stats_svc._cache.clear()
    r = FakeReports()
    app.dependency_overrides[get_reports] = lambda: r
    return r


def add_channel(client, tid, **kw):
    body = {"channel_type": "whatsapp", "display_name": "Main WhatsApp", "chatwoot_inbox_id": 7, "external_id": "pn-1", **kw}
    r = client.post(f"/admin/tenants/{tid}/channels", headers=H(ADMIN), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_stats_totals_and_per_channel(client, cw, reports):
    tid = mk_tenant(client, cw)
    add_channel(client, tid)
    r = client.get(f"/admin/tenants/{tid}/stats?ranges=month", headers=H(ADMIN))
    assert r.status_code == 200, r.text
    m = r.json()["ranges"]["month"]
    assert m["totals"]["conversations"] == 40 and m["totals"]["messages"] == 350
    ch = m["channels"][0]
    assert ch["type"] == "whatsapp" and ch["messages"] == 15 and ch["available"] is True
    assert "charged_micro" not in m["whatsapp"]                      # the customer never sees the internal figure


def test_one_failing_channel_does_not_fail_the_response(client, cw, reports):
    tid = mk_tenant(client, cw)
    add_channel(client, tid)
    reports.fail_inbox = {7}
    r = client.get(f"/admin/tenants/{tid}/stats?ranges=month", headers=H(ADMIN))
    assert r.status_code == 200
    ch = r.json()["ranges"]["month"]["channels"][0]
    assert ch["available"] is False and ch["reason"] == "http_500"
    assert r.json()["ranges"]["month"]["totals"]["available"] is True


def test_stats_are_cached_and_bad_ranges_refused(client, cw, reports):
    tid = mk_tenant(client, cw)
    client.get(f"/admin/tenants/{tid}/stats?ranges=month", headers=H(ADMIN))
    n = len(reports.calls)
    client.get(f"/admin/tenants/{tid}/stats?ranges=month", headers=H(ADMIN))
    assert len(reports.calls) == n
    assert client.get(f"/admin/tenants/{tid}/stats?ranges=forever", headers=H(ADMIN)).status_code == 422


def test_stats_need_the_admin_key(client, cw, reports):
    tid = mk_tenant(client, cw)
    assert client.get(f"/admin/tenants/{tid}/stats").status_code == 401


# ---------- top-ups ----------

def raw_invoice(**over):
    d = {"id": 14, "status": "paid", "currency": "NGN", "total": "20056.834",
         "items": [{"id": 31, "description": "WhatsApp Prepaid Wallet Top-Up", "quantity": 14, "amount": "1332.68", "user_pid": "svc-1", "addon_id": 48}],
         "payments": [{"amount_in": "20056.83", "amount_out": "0", "pmethod": "Balance"}]}
    d.update(over)
    return {"data": d}


class FakeWisecp:
    def __init__(self, raw):
        self.raw = raw

    def get_invoice(self, invoice_id):
        if isinstance(self.raw, Exception):
            raise self.raw
        return parse_invoice(self.raw)


def use_invoice(raw):
    app.dependency_overrides[get_wisecp] = lambda: FakeWisecp(raw)


def balance(client, tid):
    return client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["wallet"]["balance_micro"]


def test_audit_invoice_14_credits_subtotal_without_tax_once(client, cw):
    tid = mk_tenant(client, cw)                                       # service id svc-1, NGN
    use_invoice(raw_invoice())
    a = client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()
    assert a["results"][0]["result"] == "credited" and a["results"][0]["funding_source"] == "wisecp_balance"
    assert balance(client, tid) == 18_657_520_000                     # NGN 18,657.52: 14 x 1,332.68, tax (1,399.314) excluded
    b = client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()
    assert b["results"][0]["result"] == "already_credited" and balance(client, tid) == 18_657_520_000


@pytest.mark.parametrize("over,note", [({"status": "unpaid"}, "only paid"), ({"status": "refund"}, "only paid"), ({"total": "0"}, "zero-value")])
def test_unpaid_refunded_and_zero_invoices_credit_nothing(client, cw, over, note):
    tid = mk_tenant(client, cw)
    use_invoice(raw_invoice(**over))
    r = client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()
    assert r["results"] == [] and note in r["note"] and balance(client, tid) == 0


def test_other_lines_and_unmapped_services_never_credit(client, cw):
    tid = mk_tenant(client, cw)
    items = [{"id": 1, "description": "Account balance top-up", "quantity": 1, "amount": "5000", "user_pid": "svc-1"},
             {"id": 2, "description": "WhatsApp Prepaid Wallet Top-Up", "quantity": 1, "amount": "100", "user_pid": "svc-unknown", "addon_id": 48}]
    use_invoice(raw_invoice(items=items))
    r = client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()
    assert [x["reason"] for x in r["results"]] == ["no_tenant_for_service"] and balance(client, tid) == 0


def test_currency_mismatch_and_unresolved_currency_wait_for_staff(client, cw):
    tid = mk_tenant(client, cw)                                       # wallet is NGN
    use_invoice(raw_invoice(currency="USD"))
    assert client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()["results"][0]["reason"] == "currency_mismatch"
    use_invoice(raw_invoice(currency=3))                              # a numeric WiseCP id is not guessed
    assert client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()["results"][0]["reason"] == "currency_unresolved"
    assert balance(client, tid) == 0


def test_bank_transfer_without_payment_row_waits_for_staff(client, cw):
    tid = mk_tenant(client, cw)
    use_invoice(raw_invoice(payments=[]))
    r = client.post("/admin/topups/invoice/14", headers=H(ADMIN)).json()
    assert r["results"][0]["result"] == "needs_review" and r["results"][0]["reason"] == "paid_without_payment_record"
    assert balance(client, tid) == 0


def test_percent_discount_is_what_was_charged_and_never_rounds_up(client, cw):
    tid = mk_tenant(client, cw)
    items = [{"id": 5, "description": "WhatsApp Prepaid Wallet Top-Up", "quantity": 3, "amount": "333.33", "discount": 10, "discount_type": "percent",
              "user_pid": "svc-1", "addon_id": 48}]
    use_invoice(raw_invoice(items=items, total="1000", payments=[{"amount_in": "1000", "pmethod": "Paystack"}]))
    client.post("/admin/topups/invoice/14", headers=H(ADMIN))
    assert balance(client, tid) == 899_990_000                        # 999.99 less 10% = 899.991, cut DOWN to the kobo = 899.99


def test_wisecp_failures_are_not_credits(client, cw):
    mk_tenant(client, cw)
    use_invoice(WisecpError("invoice_not_found"))
    assert client.post("/admin/topups/invoice/99", headers=H(ADMIN)).status_code == 404
    use_invoice(WisecpError("insufficient_scope"))
    assert client.post("/admin/topups/invoice/99", headers=H(ADMIN)).status_code == 502


def test_parse_is_tolerant_but_fails_closed():
    inv = parse_invoice({"data": {"id": 9, "status": "PAID", "currency_code": "ngn", "total": 10, "items": [{"item_id": 4, "name": "x", "qty": 2, "price": 5}]}})
    assert inv.status == "paid" and inv.currency == "NGN" and inv.lines[0].line_id == "4" and inv.lines[0].subtotal == Decimal(10)
    with pytest.raises(WisecpError):
        parse_invoice({"data": "nope"})


def test_unregistered_chatwoot_inboxes_are_shown_too(client, cw, reports):
    tid = mk_tenant(client, cw)
    add_channel(client, tid)                                          # inbox 7 is registered
    reports.found = [{"id": 7, "name": "dup", "type": "whatsapp", "phone_number": None},
                     {"id": 9, "name": "Website", "type": "web_chat", "phone_number": None}]
    chans = client.get(f"/admin/tenants/{tid}/stats?ranges=month", headers=H(ADMIN)).json()["ranges"]["month"]["channels"]
    assert [c["type"] for c in chans] == ["whatsapp", "web_chat"] and chans[1]["messages"] == 15      # 7 is not listed twice
