# WiseCP payment and currency audit

Sources: WiseCP developer docs (dev.wisecp.com, read 25 Oct 2026), and your database dump (`f9P9riNtyED2.sql`: 11 invoices,
12 invoice lines, 6 payment rows, 7 orders, 5 add-on purchases, 166 currencies). The installed PHP source was NOT available.
Labels: DOC = official documentation. DB = read from your dump. INF = inference. OPEN = unresolved.

## Confirmed
| # | Finding | Basis |
|---|---|---|
| 1 | WiseCP has no webhooks and does not retry. Events are PHP hooks running inside WiseCP; the hub must poll the admin API (and/or a hook listener must push) | DOC |
| 2 | `invoice.payment_recorded` fires per payment row, even for a partial payment; it does not mean the invoice is paid. `invoice.status_changed` carries the paid/refund transition and can fire with an unchanged status | DOC |
| 3 | Admin API: a repeated `transaction_id` is refused; `Balance` takes money from the client's account balance; deleting a payment reopens the invoice and does NOT undo activated services | DOC |
| 4 | Payment rows live in `invoices_payments` (`owner_id` = invoice id, `amount_in`, `amount_out`, `fees`, `currency` (WiseCP id), `rate`, `pmethod`, `transaction_id`, `idempotency_key`, `paid_at`, `created_by`) | DB |
| 5 | A refund is a SEPARATE payment row with `amount_out` (invoice 11: 6,256.88 in, then 6,256.88 out by admin, status `refund`) | DB |
| 6 | An invoice can be `paid` with NO payment row: invoices 3, 4, 9, 12 (BankTransfer). Bank transfers are approved by hand, so the invoice status is the only signal | DB |
| 7 | An invoice can be `paid` while payments fall short of its total: invoice 10, payment 128,265.00 vs total 128,265.1802 | DB |
| 8 | Payment amounts are rounded to 2 decimals while invoices carry 4 (invoice 13: total 50,142.2355, payment 50,142.24) | DB |
| 9 | Zero totals with status `paid`: a 100% coupon (invoices 1, 2). Invoice 11 has a 10% discount. Tax (7.5%) is added on top of the subtotal (invoices 13, 14) | DB |
| 10 | WiseCP's OWN account top-ups are invoices with an "Account balance top-up" line, item currency 0, and `credit_applied_at` in `data` (invoices 4, 12). These must never credit a Desk wallet | DB |
| 11 | The Desk top-up product is add-on 48 "WhatsApp Prepaid Wallet Top-Up". Invoice 14: quantity 14 x unit 1,332.68 = 18,657.52 NGN subtotal; tax 1,399.314; total 20,056.834; paid from Balance. The line points at service 9 (`user_pid`), which maps to a hub tenant | DB |
| 12 | The unit price is USD 1 converted at the invoice rate 1,332.6841 and rounded to 2 decimals BEFORE multiplying (14 x 1,332.68, not 14 x 1,332.6841) | DB (arithmetic) |
| 13 | WiseCP prices in USD and converts at invoice creation (NGN 59,803.59 / 1,329.264 = USD 44.99 on invoice 1). The creation-time rate is kept in `data.exchange_rate`, and differs 0.1-0.3% from today's `currencies.rate` | DB |
| 14 | `currencies.rate` = units of that currency per 1 unit of the base; USD is the base (`local=1`, rate 1). In this dump NGN has `local=0` (your message said Yes) | DOC + DB |
| 15 | `currencies.format` is a display code (0 for 26 currencies, 4 for NGN), NOT a decimal count. The hub uses ISO 4217 exponents | DB |
| 16 | A client's balance has its own currency (`users.balance_currency`), which can differ from the client's currency | DB |

## Still open
- Invoice 9's `exchange_rate` is 0.00075149, the INVERSE direction (USD per NGN). Its cause is unknown, so the hub never reads `data.exchange_rate` without checking direction.
- The dump has no external gateway payments (only Balance), so gateway fees, `pmethod_commission`, chargebacks and gateway refunds are untested.
- Whether `status = refund` can be partial; whether a partial refund is simply a smaller `amount_out`.
- The tolerance WiseCP applies when marking an invoice paid (invoice 10 is 0.18 NGN short).
- Which hook fires when an admin marks a BankTransfer invoice paid (no payment row appears).

## Top-up contract (decided; implemented in Phase 4, not yet)
- **Credit = the Desk top-up line's subtotal, excluding tax**, in the invoice currency (your rule: VAT never enters the wallet).
  Invoice 14 would credit NGN 18,657.52, not 20,056.83.
- Only lines for add-on 48 that point at a Desk service count. Other lines, hosting, domains and WiseCP balance top-ups never credit.
- Trigger: the invoice reaching `paid`, then the hub reads the invoice, its lines and payments from the WiseCP API (never trusts an event payload).
- Idempotency key: `wisecp:invoice:{invoice_id}:line:{line_id}:credit`. One credit per line, ever.
- Partial payment: nothing until paid. Zero-value invoice: nothing. Discounted line: what was charged for it, not list price.
- Paid from WiseCP balance: credit (it is the customer's own prepaid money), flagged `funding_source=wisecp_balance`.
- `paid` with no payment rows or payments short of the total beyond one minor unit (BankTransfer approvals, invoice 10): credit goes to a
  **pending review** state above a staff-set threshold; below it, it credits and is flagged.
- Refund / deleted payment: reverse up to the unspent balance; any spent part becomes a receivable. Currency different from the wallet: no credit, staff alert.
