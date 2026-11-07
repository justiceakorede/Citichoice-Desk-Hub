# Setting up Citichoice Desk Hub

This is the complete first-time setup, in order. Estimated time: 1-2 hours. Nothing here needs code changes.
Every step ends with a check; do not move on until it passes. Things I could not verify from here are marked **[verify]**.

## 0. What you are deploying
Three containers from one repository: **hub** (the web app and API), **worker** (provisions Chatwoot accounts in the
background) and **db** (PostgreSQL). One app, two hostnames:

| Hostname | Serves | Protected by |
|---|---|---|
| `deskhub.citichoice.net` | staff UI only (`/ui`, `/auth`) | Cloudflare Access + Zitadel login + the `administrator` role |
| `hub.citichoice.net` | machine API only (`/admin`, `/internal`, `/healthz`) | API keys + an IP allow-list per key |

Each hostname refuses the other's routes (404), so a rule on one cannot be bypassed through the other.

## 1. What you need before you start
- [ ] The repository contents (`desk-hub-phase3a.zip` or later) pushed to a private Git repository Coolify can read.
- [ ] Coolify running with a server that has Docker. **[verify]** exact menu names differ by Coolify version.
- [ ] DNS control of `citichoice.net` (Cloudflare).
- [ ] Zitadel admin access (project "Citichoice Desk Hub", ID `396543381326529539`; application client ID `396642667028612099`).
- [ ] A Chatwoot super-admin **Platform App token** and the Chatwoot base URL.
- [ ] A WiseCP admin account (to create a read-only API key).
- [ ] A password manager. Four secrets are generated in this guide and must never go into Git or chat.

## 2. DNS and Cloudflare
1. Create two DNS records pointing at your Coolify server: `deskhub.citichoice.net` and `hub.citichoice.net` (proxied is fine).
2. `accounts.citichoice.co` must stay **DNS-only**: the hub's server calls Zitadel directly to fetch keys and swap login codes.
3. Cloudflare Zero Trust > Access > Applications: add a **self-hosted** application for `deskhub.citichoice.net` only, with a policy allowing your staff emails.
   Do NOT put Access in front of `hub.citichoice.net` (WiseCP and n8n cannot do an interactive login). Protect that host with
   a WAF rule or IP allow-list that permits only your WiseCP server and n8n server addresses.
4. If both Cloudflare and Coolify's proxy sit in front of the app, set `TRUSTED_PROXY_HOPS=2`; with Coolify's proxy only, `1`.
   This matters only for per-key IP allow-lists. Test it (section 9) before relying on it.

## 3. Zitadel
Follow `docs/ZITADEL_LIVE_CHECKLIST.md` section "Zitadel side". In short: project toggles ON, role `administrator`,
application type Web with method CODE (Basic), redirect `https://deskhub.citichoice.net/auth/callback`, post-logout
`https://deskhub.citichoice.net/auth/logged-out`, token settings "roles inside ID token" and "user info inside ID token" ON,
grant `administrator` to yourself. Copy the client secret straight into Coolify (step 5).

## 4. WiseCP read-only API key (for exchange rates)
1. In WiseCP admin, create an API credential with the **smallest read-only scope that can read financial currencies** **[verify the scope name in your version]**.
   Restrict it to the Coolify server's IP if WiseCP allows.
2. Test from any shell (replace the key; never paste it into chat):
   `curl -s -H "Authorization: Bearer $KEY" https://billing.citichoice.co/api/v1/admin/financial/currencies | head -c 400`
   You should see JSON with `code`, `local` and `rate`. An `insufficient_scope` error (documented by WiseCP) means widen the scope slightly.
3. Also confirm `.../financial/currency-settings` answers and shows a recent `last_run_at`; if WiseCP stopped refreshing rates, fix that first.

## 5. Deploy in Coolify
1. New resource from your Git repository, **Docker Compose** build, file `docker-compose.yml`. **[verify]**
2. Domains: on the **hub** service set `https://hub.citichoice.net,https://deskhub.citichoice.net` (port 8000). The `worker` and `db` services get no domain.
3. Environment variables (Coolify > Environment). Generate secrets with `python -c "import secrets;print(secrets.token_urlsafe(48))"`:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | new strong password |
| `SECRET_KEY` | new, 32+ characters (signs login cookies) |
| `PUBLIC_URL` | `https://hub.citichoice.net` |
| `STAFF_UI_HOST` / `API_HOST` | `deskhub.citichoice.net` / `hub.citichoice.net` |
| `OIDC_ISSUER` | `https://accounts.citichoice.co` (exactly; no trailing slash) |
| `OIDC_CLIENT_ID` | `396642667028612099` |
| `OIDC_CLIENT_SECRET` | from Zitadel (secret) |
| `OIDC_REDIRECT_URI` / `OIDC_POST_LOGOUT_URI` | the two URLs from step 3 |
| `OIDC_PROJECT_ID` / `OIDC_REQUIRED_ROLE` | `396543381326529539` / `administrator` |
| `CHATWOOT_URL` / `CHATWOOT_PLATFORM_TOKEN` | your Chatwoot (token is secret) |
| `WISECP_API_URL` / `WISECP_API_KEY` | `https://billing.citichoice.co` / the read-only key (secret) |
| `TRUSTED_PROXY_HOPS` | `1` or `2` (section 2) |

   Mark the secrets as secret/hidden. The full list with comments is `.env.example`. In production the app **refuses to start**
   if anything essential is missing or inconsistent, and the log says which variable.
4. Deploy. The hub container runs `alembic upgrade head` on every start, so the database schema is created automatically.
   **Check:** `https://hub.citichoice.net/healthz` returns `{"ok":true}`, and the same path works on `deskhub.citichoice.net`.
   `https://deskhub.citichoice.net/admin/tenants` and `https://hub.citichoice.net/ui` both return 404 (host separation working).
5. Check the worker: its log shows `worker-xxxxxx started; polling for jobs` and then stays quiet until there is work; each job it runs is logged by id and result. It handles Chatwoot provisioning jobs.

## 6. First-run configuration
Open a terminal in the **hub** container (Coolify > hub > Terminal) **[verify]** and run:

1. **Currencies.** All ISO currencies are loaded but billing is OFF for each. Enable the ones you bill in (these are your active WiseCP currencies):
   `python -m app.cli enable-currencies USD NGN EUR GBP AUD CAD CHF CNY HKD INR JPY KES MXN NOK NZD PLN SEK SGD ZAR BRL CLP COP DOP EGP GHS XAF XOF`
   Do not enable `GGP IMP JEP HRK ZWL ANG BGN CUC LTL SLL VEF YUN`: WiseCP knows them but they are not in the current ISO list.
2. **Exchange rates.** `python -m app.cli fx-sync`. Expect `fx-sync: {'stored': N, ...}`. If it fails, the message names the reason (section 10).
   A rate that moves more than 25% since the last one is held for approval: staff approve it at `/ui/rates`.
3. **Rate cards (what Meta charges per message).** In the staff UI `/ui/rates`, add one card per category (marketing, utility,
   authentication, service) with market `*`, currency = the currency Meta bills you in, the per-message figure **copied from Meta's current
   rate card page for your account**, and the source URL. Add country-specific cards (e.g. `NG`) where Meta's price differs.
   Never type a figure from memory: customers are charged from these numbers.
4. **Plans.** Four plans are pre-loaded (starter, growth, business, scale). Prices live in WiseCP, not here. Edit limits and default
   markups via `PATCH /admin/plans/{code}` if needed; changes apply to every tenant on that plan and re-sync their Chatwoot limits.
5. **API keys for machines** (each is shown once; store it immediately):
   - WiseCP module: `python -m app.cli create-key wisecp-module --scope admin --ip <WiseCP server IP> --days 365`
   - n8n, one per tenant it may touch: `python -m app.cli create-key n8n-benny --scope internal --tenant <tenant-slug>`
   An internal key with no tenant is refused by design.
6. **Schedule the recurring jobs** (Coolify > Scheduled Tasks **[verify]**), each running `python -m app.cli ...` in the hub container:
   | Command | When |
   |---|---|
   | `fx-sync` | hourly |
   | `expire-reservations` | every 5 minutes (releases holds on sends that never got an outcome, after 72 hours) |
   | `escalate` | daily (locks tenants whose suspension grace period ended) |

## 7. Smoke test (about 10 minutes)
- [ ] Sign in at `https://deskhub.citichoice.net/ui` (Zitadel) and see the tenant list.
- [ ] `/ui/rates` shows your rate cards and recent FX rows with source `wisecp:currencies`.
- [ ] Create a test tenant through the API on the hub host (needs the wisecp-module key):
      `curl -X POST https://hub.citichoice.net/admin/tenants -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"wisecp_service_id":"test-1","billing_currency":"NGN","name":"Test Co","owner_email":"you@citichoice.co","plan":"growth"}'`
      Within a minute the tenant's status becomes `active` (worker provisioned Chatwoot). If it ends `provisioning_failed`, see `GET /admin/jobs?status=dead,blocked`.
- [ ] Preview a price: `GET /admin/pricing/preview?tenant_id=<id>&category=utility&country=NG`. The answer shows provider cost, FX buffer, fee and total.
- [ ] Credit the wallet: `POST /admin/wallets/<id>/credit {"amount":"1000","currency":"NGN","idempotency_key":"smoke-1"}`; repeat the call and confirm the balance does not change.
- [ ] Sign in with a user who does NOT hold the role: refused.
- [ ] Clean up: cancel the test tenant (`POST /admin/tenants/<id>/cancel`); when it reads `cancelled`, terminate it (`POST /admin/tenants/<id>/terminate?confirm=<its slug>`). Termination keeps the locked Chatwoot account and the wallet history; nothing is deleted.

## 8. Local development
`python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`
`python -m pytest` runs on SQLite. For the full suite start a PostgreSQL 16 and run
`TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/deskhub_test python -m pytest`.
The staff UI always requires Zitadel (there is deliberately no password back door). For local sign-in, create a **second** Zitadel application
with Development Mode on and redirect `http://localhost:8000/auth/callback`; run with `ENV=development` and leave `STAFF_UI_HOST`/`API_HOST` empty.
**[verify]** browsers accept `__Host-` cookies on `http://localhost`; if not, use a local https tunnel.

## 9. Security checks before going live
- [ ] `ENV=production`; secrets only in Coolify; `.env` not in Git (`.gitignore` covers it).
- [ ] Cloudflare Access on `deskhub` only; WAF/IP allow-list on `hub`.
- [ ] Send a request to the API host with a fake `X-Forwarded-For` header from a non-allowed address using a key restricted to an IP: it must be refused. If it is accepted, your `TRUSTED_PROXY_HOPS` is wrong.
- [ ] `CUSTOMER_API_ENABLED=false` (default).
- [ ] Database backups configured and one restore tested (docs/OPERATIONS.md).
- [ ] You have run docs/ZITADEL_LIVE_CHECKLIST.md end to end.

## 10. When something is wrong
| Symptom | Cause and fix |
|---|---|
| App will not start, log says `Unsafe production configuration` | a required variable is missing or inconsistent; the message names it |
| Sign-in shows "Access denied" for a valid admin | role missing or the claim name differs. Check the hub log line `role claim names present`; see the Zitadel checklist |
| `404` on a page | wrong hostname for that route (section 0) |
| `fx-sync FAILED (provider_stale)` | WiseCP stopped refreshing rates, or `last_run_at` is missing; fix it in WiseCP > Currencies |
| `fx-sync FAILED (base_changed)` | WiseCP's base currency is no longer `WISECP_BASE_CURRENCY`; rates changed meaning, so the hub refuses them. Set it back or update the variable deliberately |
| `fx-sync FAILED (provider_refused)` | wrong key, wrong scope, or IP not allowed |
| Usage events answer `422 no_rate_card` | no rate card for that category/country/currency on that date: add one at `/ui/rates` |
| `422 fx_stale` / `no_fx_rate` | exchange rates are older than 48 h or missing: run `fx-sync`, check the schedule |
| A rate shows `quarantined` | it moved more than 25%; if genuine, Approve it at `/ui/rates` |
| Tenant stuck in `provisioning_failed` or job `blocked` | `GET /admin/jobs?status=dead,blocked`; a `blocked` job means Chatwoot's reply was unclear; check Chatwoot for the account, then `POST /admin/jobs/<id>/retry` with `chatwoot_account_id` (adopt) or `confirm_not_created:true` |
| Migration aborts with `ABORTED: wallet balance differs from ledger` | the database is unchanged; investigate that tenant's ledger before retrying |
