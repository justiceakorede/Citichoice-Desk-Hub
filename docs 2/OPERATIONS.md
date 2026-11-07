# Operating Desk Hub

## Moving parts and what to watch
| Part | Normal state | If it is not |
|---|---|---|
| hub container | `/healthz` returns `{"ok":true}` on both hostnames | check the log; production refuses to start on bad config and says why |
| worker container | log: `started; polling for jobs`; quiet when idle | restart it; pending jobs are safe, nothing is lost (leases expire after 5 minutes) |
| db (PostgreSQL) | healthy; volume `pgdata` mounted | see Backups. Never delete the volume |
| `fx-sync` schedule (hourly) | `GET /admin/fx?limit=5` shows a row from the last hour per pair | rates older than 48 h make pricing refuse messages (`fx_stale`) |
| `expire-reservations` schedule (every 5 min) | `python -m app.cli expire-reservations` prints `expired 0 reservation(s)` most of the time | stale holds keep money reserved for up to 72 h; run it by hand |
| `escalate` schedule (daily) | suspended tenants past the grace period become locked | run it by hand once: `python -m app.cli escalate` |

## Daily and weekly checks (5 minutes)
- `/ui` tenant list: any wallet showing `zero`, `critical` or `low`? (`GET /admin/alerts` returns the same list for automation.)
- `GET /admin/jobs?status=dead,blocked`: should be empty. A `blocked` job needs a human (SETUP.md section 10).
- `/ui/rates`: any `quarantined` FX rate waiting for approval? Until approved it is NOT used.
- `/ui/audit`: skim failed logins (`staff.login` failure) and `fx.sync` failures.
- Weekly: `python -m app.cli list-keys`; revoke anything unused.

## Staff access
- Staff sign in with Zitadel; the hub enforces the `administrator` role on every action and logs who did what.
- **Removing someone's access:** remove their `administrator` grant in Zitadel. Their current session stays valid until it expires
  (30 minutes idle, 8 hours at most), but they cannot sign in again. For an emergency, restart the hub with a changed `SECRET_KEY`
  (this invalidates login cookies; sessions are also revocable in the `staff_sessions` table).
- Changing the role name needed for access: set `OIDC_REQUIRED_ROLE` and redeploy. It is checked on every request.

## Machine API keys
- Create: `python -m app.cli create-key NAME --scope admin|internal ...` (shown once). Inspect: `list-keys`. Revoke: `revoke-key NAME`.
- Rotate every 12 months (default expiry is 365 days): create the new key, update the caller (WiseCP module / n8n), confirm traffic, then revoke the old one.
- `admin` keys are for the WiseCP module only and should carry an IP restriction. n8n keys are `internal` and each is bound to specific tenants.

## Manual money changes: maker-checker
Every manual change to a wallet or a debt is a **request** in the staff UI (tenant page > "Propose a money adjustment", or `/ui/adjustments`):
| Kind | Effect when approved |
|---|---|
| Credit funds | adds funds (clearing any debt first), e.g. compensation |
| Take funds back | removes available funds only; can never make the wallet negative or touch funds held for sends |
| Write off debt | forgives part of what the tenant owes (no money moves; recorded in the receivable ledger) |
| Add a missed charge | charges the tenant: funds first, the rest becomes debt |
| Reverse one message charge | refunds exactly that charge (by message id), once; debt is reduced first |
- The proposer gives a reason and an evidence reference (ticket, invoice or email id). **A different staff member approves or rejects.** An approval posts the entry in the same step; a rejection moves nothing. Posted requests are never edited.
- **If you are the only approver:** set `ADJUSTMENT_SELF_APPROVAL_LIMIT_USD` (default 0 = never). Then you may approve your OWN request up to that USD amount (converted at the current rate; refused if no rate exists). Each such approval is flagged and audited. Larger ones still need a second person.
- The audit log records both people, the amount and the evidence. No API key can adjust a wallet; the old `/admin/wallets/<id>/adjust` endpoint is gone.
- Reversing a charge does not edit the usage record or its pricing snapshot; the refund is a new ledger entry linked to it.

## The AI-reply allowance (for whoever builds the n8n workflows)
- Before the AI sends a reply: `POST /internal/ai-reply {"key": "<stable id of THIS reply>", "count": 1}`. Use something that survives a retry, such as the n8n execution id plus the conversation message id. Same key twice = counted once.
- If `allowed` is false, do not send the AI reply; hand the conversation to a person.
- If generation fails before anything is sent: `POST /internal/ai-reply/<key>/release`. Retrying that reply later claims again and counts once.
- Staff corrections: `POST /admin/tenants/<id>/ai-adjust` (reason required, audited). The audit trail is `GET /admin/tenants/<id>/ai-consumptions`.

## Debt, credit limits and holds (how a wallet really works)
A wallet has four numbers: **funds** (never negative), **owes** (the receivable), **held** (reserved for sends in flight) and a
**credit limit** (default 0). What a tenant may still START sending is: funds - owes - held + credit limit.
- **Funds and debt never coexist.** If Meta charges more than the tenant has, the funds pay what they can and the rest becomes **owes**, an explicit debt with its own ledger (`GET /admin/wallets/<id>/receivable`). A payment clears the debt first; only the rest becomes funds.
- **The provider's charges are always recorded**, even for suspended or terminated tenants and even beyond the credit limit. Recording a charge never lets the tenant send more.
- **New sends the hub controls** (`POST /internal/reservations`) are approved only while funds plus credit cover them, held atomically so two callers cannot spend the same money. Marketing sends must rest on the tenant's own funds, never on credit.
- **Sends the hub cannot control** (for example a native Chatwoot campaign) are not held. They are charged after the fact, so exposure for those is limited by monitoring, not by a hard stop. `send-check` is advisory only.
- **Setting a credit limit** is a finance decision: `PATCH /admin/wallets/<id>/credit-limit {"amount":"50000","currency":"NGN","reason":"..."}` (audited with before and after). Use 0 to switch credit off.
- **Watch** `GET /admin/alerts`: `debt` means a tenant owes money; chase the payment or suspend. A staff debit can only take available funds, so it can never create debt.
- **Changing money by hand** (see the next section) always needs a reason, evidence and a second person. Do not edit the database.

## Prices, rates and exchange rates (the money inputs)
- **Rate cards** are versioned and append-only. To change a price, add a new version starting on a future date; it closes the previous one. Backdating and edits are refused by the application AND by the database. A card entered by mistake can be withdrawn (`/admin/rate-cards/<id>/deactivate`); usage already priced from it keeps its own frozen snapshot.
- **Meta changes its rates on fixed dates (1 Jan/Apr/Jul/Oct)**: add the new cards before that date, effective that date.
- **Exchange rates** are append-only too. A manual rate is the fallback if WiseCP's refresh stalls: `POST /admin/fx {"quote":"NGN","rate":"1600.5"}` or the form at `/ui/rates`. It is used immediately unless it moves more than 25%.
- Every charge points at an immutable **pricing snapshot** showing the exact provider price, exchange rate (with source and time), buffer, markup and rounding rule. To explain any charge to a customer, open the usage row's snapshot.
- Customers' wallet amounts are exact (micro-units); the UI rounds only for display.

## Backups and restore
**What to back up:** the PostgreSQL database only (the app is stateless). It holds the money: wallets, ledger, usage, snapshots, audit log.
1. **Schedule:** in Coolify enable scheduled backups on the database with off-server storage (S3-compatible) **[verify]**, at least daily. Keep 30 days.
   Manual: `docker exec <db-container> pg_dump -U hub -Fc deskhub > deskhub-$(date +%F).dump`
2. **Restore drill (do this once before launch, then quarterly):**
   - start an empty PostgreSQL 16; `createdb deskhub_restore`
   - `pg_restore -U hub -d deskhub_restore --no-owner deskhub-YYYY-MM-DD.dump`
   - run the hub against it (`DATABASE_URL=...deskhub_restore`) and open `/ui`;
   - verify money: `select t.id from wallets w join tenants t on t.id=w.tenant_id left join (select tenant_id, sum(amount_micro) s from ledger_entries group by tenant_id) l on l.tenant_id=w.tenant_id where coalesce(l.s,0) <> w.balance_micro;` must return **no rows**.
3. **Recovery objective to confirm with your hosting:** with daily dumps you can lose up to a day of ledger entries. If that is not acceptable, enable continuous archiving (WAL) on the database.

## Upgrades and rollback
1. Take a backup. Read the release notes in `docs/IMPLEMENTATION_STATUS.md` ("Breaking changes").
2. Deploy the new version. The hub applies database migrations automatically on start.
3. Run the smoke test (SETUP.md section 7).
4. **Rollback of code** is safe only if the migration did not change the schema, so check the release notes. If it did, restore the backup taken in step 1 into a fresh database and redeploy the old version against it.
   `alembic downgrade` exists for every migration, but it deliberately **refuses** (changing nothing) when it would lose information, e.g. amounts finer than a currency's minor unit, non-USD rate cards, or quarantined FX rows. Treat a downgrade as a last resort.

## Incident quick reference
| Situation | Do this |
|---|---|
| Customers' messages are being refused with `fx_stale` | `python -m app.cli fx-sync`; if WiseCP is the problem add a manual rate (`/ui/rates`) and fix WiseCP |
| A wallet looks wrong | `GET /admin/wallets/<id>/ledger`; the balance always equals the sum of the ledger, and `owes` equals the sum of the receivable ledger. Correct only with `POST /admin/wallets/<id>/adjust` (needs a reason; audited). Never edit the database by hand |
| A top-up was credited twice | impossible by design (idempotency key); if you see it, stop and investigate before adjusting |
| A staff account is compromised | remove the Zitadel grant, change `SECRET_KEY`, review `/ui/audit`, revoke API keys that person could reach |
| Chatwoot is down | provisioning jobs retry with backoff and then wait in `dead`; retry them later via `/admin/jobs/<id>/retry` |
