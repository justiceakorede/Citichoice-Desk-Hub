# Implementation status

Last updated: after Phase 3.8. NOT production-ready.

## Test results (actual runs)
- PostgreSQL 16, whole suite: **377 passed, 0 failed, 0 skipped.**
- SQLite (foreign keys enforced), whole suite: 358 passed, 19 skipped (the PostgreSQL-only tests).
- No expected failures remain.
- MOCK vs LIVE: every Zitadel, Chatwoot, WiseCP and Meta test uses a mock or fake. Nothing has run against a live system.
  Live checks you must run: docs/ZITADEL_LIVE_CHECKLIST.md, and `python -m app.cli fx-sync` against your WiseCP (docs/SETUP.md section 4 and 6).

## Done
- **3.1 Staff sign-in (Zitadel OIDC):** strict token validation, server-side sessions, per-request `administrator` role check, CSRF + Origin, host separation, customer routes off.
- **3.2 Currencies:** ISO 4217 table, micro-unit money everywhere, billing currency per tenant, currency-checked wallet operations, migrations 0003-0004.
- **3.3 Rates, FX, snapshots (this step):**
  - Versioned rate cards with a database-enforced no-overlap rule; frozen amounts; backdating refused.
  - Append-only FX observations; quarantine of moves over 25%; refusal of stale (48 h) or missing rates; WiseCP adapter that fails closed (mock-tested).
  - Immutable pricing snapshots; every billed message points at one; the provider's ACTUAL cost is now separate from the FX buffer, fee and total.
  - Security fix: the usage endpoint no longer accepts a caller-supplied price (it could be used to undercharge).
  - Migration 0005 converts legacy rate cards, FX and usage (with reconstructed snapshots) and is tested up/down on PostgreSQL with legacy data.
  - **3.4 Message lifecycle and event identity:** every status event (sent, delivered, read, failed) is kept in `message_events`; only the billable status (setting `BILLABLE_STATUS`, default `delivered`) creates a charge; usage is unique per (provider, message id); a message id cannot be used by two tenants. Ten concurrent identical events charge once (PostgreSQL test). Migration 0006 tested.
  - **3.5 Receivable and credit limit:** the wallet can never go negative (database constraints). Charges the provider already made are always recorded; funds pay what they can and the rest is an explicit receivable with its own append-only ledger. Funds and debt never coexist, and a payment clears debt first. A per-tenant credit limit (default 0, audited changes) gates NEW controlled sends only. Both ledgers are append-only by database trigger. Legacy negative wallets are reclassified visibly by migration 0007.
  - **3.6 Reservations:** `POST /internal/reservations` holds funds plus granted credit atomically (idempotent on `send_id`); `/release`; automatic settlement when the usage event carries the same `send_id`; expiry after 72 h. Marketing must rest on the tenant's own funds. Proven under 60 concurrent holds (exactly the affordable number succeed) and 300 mixed concurrent operations (all invariants hold) on PostgreSQL.
  - **3.7 AI allowance:** every logical AI reply is claimed with a caller-supplied stable key (`POST /internal/ai-reply {key, count}`), so a retried n8n workflow counts once; a failed generation is released (`POST /internal/ai-reply/{key}/release`) and a retry of it counts once more; the limit is exact under 30 concurrent claims; paid packs survive a plan change; the month follows Lagos time; staff corrections are keyed, bounded at zero and audited (`POST /admin/tenants/{id}/ai-adjust`). Migration 0008 carries existing counts over as records.
  - **3.8 Maker-checker adjustments:** five kinds of manual money change (credit, take back, write off debt, add a missed charge, reverse one message charge) are requests with a reason and evidence, approved by a DIFFERENT staff member (database CHECK plus application rule). Optional small-amount self-approval (default off). Posting is keyed, idempotent and audited; simultaneous approvals post once (PostgreSQL test). A message charge can be reversed once (`usage_reversals`); usage records and snapshots are never edited. The machine-key `/adjust` endpoint was removed. Migration 0009.
  - Setup and operations documentation (SETUP.md, OPERATIONS.md), key-management CLI (`list-keys`, `revoke-key`), `fx-sync`.

## Known limitations
1. **Sends the hub cannot control** (Chatwoot native campaigns, agent replies, anything that does not call `/internal/reservations`) are NOT held or blocked. Their charges are recorded afterwards as debt. Whether Chatwoot's send paths can be metered or stopped is untested (needs a live Chatwoot). `send-check` is advisory.
1b. There is no automatic suspension when debt passes a threshold; alert `debt` is the signal.
2. The FX buffer is one global percentage, applied even when no conversion happens (e.g. a USD wallet on USD prices). A per-currency pricing policy table is not built yet.
3. The provider billing currency is one global setting (`PROVIDER_CURRENCY`). Per-WABA currencies arrive with the channel/WhatsApp-number work.
4. **Every usage cost is an ESTIMATE** (rate card x exchange rate at message time). The provider's actual figure arrives with Meta's webhooks/reports and is reconciled in step 3.9 (an append-only `usage_finalizations` record, not an edit). Which status Meta/Chatwoot actually sends, and whether `delivered` is the billable one, is unverified: that is why it is a setting.
5. The WiseCP FX adapter is built from WiseCP's documentation and a mock. The API key scope name and the real response are unverified.
6. Cross-currency conversion goes through USD only; the latest rate at or before the message time is used (no interpolation).
7. Reconciliation, including actual-vs-estimated cost (3.9). Reversal of a TOP-UP (refund/chargeback from WiseCP) arrives with the WiseCP integration (Phase 4); a partial reversal of a message charge is not supported (full only).
7b. n8n workflows must be written to send a stable `key` per reply and to release it on failure; the hub cannot infer either. The ledgers ARE append-only by database trigger (3.5).
8. Sessions are not revoked at Zitadel logout elsewhere (no back-channel logout yet); a removed role takes effect at next sign-in.
9. Chatwoot, WiseCP payments, Meta: unverified live. Docker image and compose unbuilt. No rate limiting.
10. Terminate keeps the locked Chatwoot account (no retention policy). Customer routes are off.

## Breaking changes since Phase 2
- `POST /admin/tenants` needs `billing_currency`. Credits/adjustments take `amount` + `currency`. API money fields are `*_micro`.
- `POST /admin/wallets/{id}/adjust` is REMOVED. Manual money changes are maker-checker requests in the staff UI.
- `/internal/ai-reply` now REQUIRES a `key` (stable per logical reply), capped at 1,000 units per claim.
- `/admin/rates` and the old `/admin/fx` are replaced by `/admin/rate-cards`, `/admin/fx`, `/admin/fx/sync`, `/admin/fx/{id}/approve`.
- Usage events reject unknown fields (including any price). Usage responses give `provider_cost_micro`, `fx_buffer_micro`, `service_fee_micro`, `charge_micro`.
- Production needs the Zitadel, host and WiseCP variables in `.env.example`.

## Next
3.9 reconciliation (including actual-vs-estimated cost). Then Phase 4 (WiseCP top-up integration, per docs/WISECP_AUDIT.md).

## Added after Phase 3.8: customer stats and WiseCP top-ups (NOT RUN, NOT VERIFIED LIVE)
Written in a sandbox with no Python dependencies and no network. `python -m pytest` has NOT been run on any of this, including the
new `tests/test_stats_topups.py`. Run the whole suite first and fix whatever it finds.
- `GET /admin/tenants/{id}/stats?ranges=month,30d` (admin key): conversations and messages (total and per channel) from Chatwoot
  reports, WhatsApp delivered/charged from the hub's own usage records, and a customer-safe wallet block. A failing Chatwoot call
  shows `available:false` with a reason, never zero. Results are cached per process for `STATS_CACHE_SECONDS`.
  [UNVERIFIED] the Chatwoot endpoint `GET /api/v2/accounts/{id}/reports/summary` and its field names; needs the reporter user.
- `POST /admin/topups/invoice/{id}` (admin key): reads the invoice from WiseCP and credits per the audit's top-up contract
  (paid only, line subtotal excluding tax, one credit per line, wallet currency must match, anomalies wait for staff).
  [UNVERIFIED] the JSON shape of `GET /api/v1/admin/invoices/{id}`, how an add-on line is identified, and how the invoice currency
  is expressed. `python -m app.cli wisecp-invoice 14` must print credit 18,657.52 for your invoice 14 before this is trusted.
- CLI: `wisecp-invoice`, `topup-invoice`, `add-reporter`.
- STILL NOT BUILT: reversal when WiseCP refunds or deletes a top-up payment; a held ("needs_review") top-up has no staff screen
  yet (it is an audit row `topup.needs_review`; credit it through the existing maker-checker adjustment, then re-run is safe because the
  adjustment uses its own key, so check the ledger first); polling as a fallback when the WiseCP hook does not fire.

## Added later (all NOT RUN)
- Migration 0010: a staff member can reject (withdraw) their own adjustment request. The old rule needed a second person for any decision, so a sole admin rejecting got a 500.
- Staff UI: modern, responsive, dark-mode-aware layout; new **Plans** page (seats, channels, AI replies, markup, KB level, active). Saving enqueues `sync_limits` for every active tenant on the plan.
- `PUT /admin/tenants/{id}/owner-password` (Platform API `PATCH /users/{id}`), `Chatwoot.user_token()` so stats read reports as the tenant's own owner (no extra seat). [UNVERIFIED] both Platform API behaviours.
- `message_prices` in stats: customer per-message price per category (default market).
- WiseCP Server module replaces the Product module.

## Meta billing (NOT RUN)
`python -m app.cli meta-billing-sync` bills WhatsApp usage from Meta's daily pricing analytics (one usage row per phone number, day, category
and country, fixed key so it can never double-charge). Needs META_ACCESS_TOKEN and META_BILLING_START, and a registered channel with
phone number and WABA id (`add-channel`). Migration 0011 adds usage_records.quantity. Balance falls once a day, ~36h behind. There is
still no real-time blocking: n8n does not call send-check/reservations, so a wallet can go negative (a receivable).
[UNVERIFIED] the Graph pricing_analytics field syntax and response shape. Use --dry-run first.

## Credit gate, instant debit, notifications (ALL NOT RUN)
- n8n workflow (BennyBoom-AI-replies-hub-counted.json): before an AI reply it reserves one service message (`/internal/reservations`); with no
  credit it sends nothing and hands the chat to a person. After each message posts it charges the wallet at once (`/internal/usage`, id `n8n:...`).
  Team WhatsApp alerts are checked with `/internal/send-check` and charged as utility messages. If the hub is unreachable the bot keeps answering.
- The daily Meta job now trues up per (customer, day, category): it charges only messages not already charged, and never refunds an over-estimate.
- Messages typed by agents or sent by Chatwoot Campaigns are NOT gated; they are charged by the daily job. A wallet can therefore go negative.
- Weekly report and low-balance alert: see docs/NOTIFICATIONS.md. Migrations 0011 (usage quantity) and 0012 (tenant notify settings).
