# Add Funds and automatic recharge

The customer's WhatsApp wallet card (service page, Citichoice Desk services only) has two parts:

* **Add Funds**: pick a preset or type an amount, the hub raises a top-up invoice in the wallet's own currency, the customer pays it on the
  normal WiseCP invoice page with any gateway.
* **Automatic recharge**: when the wallet falls below a threshold, the hub raises a top-up invoice and WiseCP settles it from the
  customer's **Account Balance**. The customer never has to log in to keep WhatsApp running.

## How the money moves (nothing new is trusted)

```
card -> module (use_clientArea_wallet_*) -> hub  -> WiseCP admin API: POST /invoices (line = "WhatsApp Prepaid Wallet Top-Up", user_pid = service)
                                                  -> hub worker polls the invoice every minute
                                                  -> status paid  => wisecp.process_invoice  (the existing, once-only, tax-excluded credit)
```
* The wallet is credited **only** by the existing invoice crediting. It re-reads the invoice from WiseCP, needs `paid`, credits the line
  subtotal excluding tax, once (idempotency key per invoice line). Auto-recharge and Add Funds add nothing to that trust boundary.
* Invoices are created in the wallet's currency, so the USD/NGN mismatch cannot happen on this path.
* Customers pay VAT on top, exactly like the add-on today; the wallet gets the amount without tax.

## Safeguards (automatic recharge)
* Customer opt-in with their own threshold, amount and monthly limit; the hub refuses amounts outside USD 5 .. 2,000 equivalent (settable).
* One open recharge per tenant (database unique index), at least 10 minutes between recharges, at most 3 per day, monthly cap enforced.
* An invoice not paid from the Account Balance within 30 minutes is cancelled and counts as a failure; 3 failures in a row pause
  automatic recharge and the customer is emailed. Saving the card again resumes it.
* Every step is in the audit log (`recharge.invoice`, `recharge.failed`, `recharge.cap_reached`, `auto_recharge.save`).
* Customer emails (to the workspace's notification email addresses): topped up, failed, paused, monthly limit reached.

## Setup
1. Deploy the hub (this release adds migration `0015`; run `alembic upgrade head` or let the container do it). The **worker** container now
   settles invoices every minute and needs WiseCP and mail settings: they are in `docker-compose.yml` already (WISECP_*, SMTP_*, RECHARGE_*).
2. The WiseCP **admin API key** used by the hub must be allowed to **create invoices** (and to change an invoice's status, used to cancel an
   unpaid automatic recharge). Today it only reads invoices.
3. Upload the module (`DeskHub/`) and the theme (`CitichoiceHost/`).
4. Optional env: `WALLET_PRESETS_JSON` (add the line to the compose `environment:` yourself), `RECHARGE_MIN_USD`, `RECHARGE_MAX_USD`,
   `RECHARGE_MAX_FAILURES`, `RECHARGE_PAY_TIMEOUT_MINUTES`, `RECHARGE_MIN_GAP_MINUTES`, `RECHARGE_DAILY_LIMIT`.
5. For automatic recharge the customer needs funds in **Account Balance** and **Automatic Payments** switched on there (WiseCP settles the
   invoice from the balance; the hub does not debit it).

## Verify on the real WiseCP, in this order (these calls are written from WiseCP's public API docs and were not run against your install)
1. Staff: `POST /admin/tenants/{id}/add-funds` with a small amount and the customer's WiseCP id. An unpaid invoice must appear for the right
   customer, on the right service, in NGN. If WiseCP refuses, the error code is in the response and the audit log.
2. Pay it with any gateway. Within about a minute the wallet must be credited once (`python -m app.cli recharge-run` forces a pass).
3. Enable automatic recharge for a test tenant with a high threshold. A paid-from-balance invoice must appear and be settled by WiseCP;
   if it stays unpaid, check Automatic Payments, the `Balance` payment method name, and how WiseCP times the payment (the hub waits 30 minutes).
4. Cancel path: let one recharge go unpaid; it must be cancelled in WiseCP. If WiseCP rejects the cancel call the hub logs it and still
   counts the failure.
5. Module: check the customer id is found (`client_id()` in `DeskHub.php`) and that `run_module_method('wallet_settings')` returns the card data.

## Rollback
Switch automatic recharge off per tenant (the card, or `PUT /admin/tenants/{id}/auto-recharge` with `enabled:false`). The old top-up add-on
keeps working untouched, so nothing else depends on this feature.

## Not built
Saved-card auto-charge (gateways that need a browser step such as OTP/3-D Secure cannot be charged unattended; decide per gateway later),
refund reversal of a top-up credit (same gap as the add-on), WhatsApp notifications for recharge events (email only).
