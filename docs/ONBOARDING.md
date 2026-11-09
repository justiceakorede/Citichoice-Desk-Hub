# Onboarding guide: platform setup once, then each client

Everything here is NOT RUN on a live system. Commands run in the **hub container** (Coolify > hub app > Terminal) unless it says otherwise.
Check any client at any time with `python -m app.cli onboard-check <slug>`: it prints what is OK and exactly what to fix.

## PART A: once for the whole platform

### A1. Deploy
1. Replace the repo contents with `desk-hub-updated.zip` (upload what is INSIDE the `desk-hub` folder to the repo root), commit, push, redeploy.
2. Run migrations once: `alembic upgrade head` (should end at revision 0012). Check: `python -m app.cli list-tenants` runs without error.

### A2. Coolify environment variables on the hub app (then redeploy)
| Variable | Value |
|---|---|
| `META_ACCESS_TOKEN` | A Meta system-user token with `whatsapp_business_management`, assigned to every WABA you bill (see A3) |
| `META_WABA_TOKENS` | Only if some WABA is in a different Meta business: `{"<waba id>":"<token>"}` |
| `META_BILLING_START` | First day to bill, e.g. `2026-11-09`. Nothing earlier is ever charged. |
| `SERVICE_MESSAGES_FREE` | `false` |
| `PRICE_DISPLAY_MARKET` | `NG` |
| `NOTIFY_WA_PHONE_NUMBER_ID` | The Phone number ID of YOUR Citichoice WhatsApp number (A4) |
| `NOTIFY_WA_TOKEN` | Leave empty to reuse `META_ACCESS_TOKEN` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Your email provider |
| `ADJUSTMENT_SELF_APPROVAL_LIMIT_USD` | While you are the only admin, a large number such as `100000` |

### A3. The Meta token (answers "is it added to the tenant?")
It is NOT stored per tenant. The hub has no encrypted storage, so it reads tokens from environment variables.
- One system-user token reaches every WABA that was assigned to that system user in Meta Business Settings. Your clients' numbers all sit in
  your business, so ONE token covers them. Bennyboom and Kynetiq already share one in n8n.
- Check coverage: Meta Business Settings > Users > System users > your user > Assigned assets: every WABA listed, with "Manage" or at least view
  access to the WhatsApp account, and the token must have `whatsapp_business_management`. If it does not, generate a new token with that permission
  and put it in `META_ACCESS_TOKEN` (do not reuse a token that only has `whatsapp_business_messaging`).
- A client whose WABA lives in THEIR OWN Meta business (or after you become a Solution Partner) needs their own token: add it to
  `META_WABA_TOKENS` with that WABA's id. `onboard-check` tells you if a WABA has no token.

### A4. Your own WhatsApp number for reports (answers "how does it get my credentials?")
The reports use exactly two things you give once: `NOTIFY_WA_PHONE_NUMBER_ID` and a token (`NOTIFY_WA_TOKEN`, or `META_ACCESS_TOKEN`).
1. Meta Business Settings > WhatsApp accounts > your Citichoice WABA > Phone numbers: open your number; copy **Phone number ID** (digits, not the +234 number).
2. Create the two Utility templates from `docs/NOTIFICATIONS.md` in that same WABA and wait for approval.
3. Put the Phone number ID in `NOTIFY_WA_PHONE_NUMBER_ID`. The hub then calls Meta as that number. The number can be the same one your own Citichoice bot uses.
These messages are billed to YOUR WABA by Meta (a utility message, a fraction of a naira each); they are not charged to customers.

### A5. Currencies, FX and rates
1. Desk Hub > Rates & FX: enable the currencies you bill in (NGN, USD). Or `python -m app.cli enable-currencies NGN USD`.
2. `python -m app.cli fx-sync`
3. `python -m app.cli seed-meta-rates`   (Meta's per-country rates, USD, effective today)
4. Desk Hub > Plans: set seats, channels, AI replies and default markup per plan.

### A6. Chatwoot Platform App access (existing accounts only)
New customers made by WiseCP need nothing. Accounts that already existed must be granted to the Platform App once. In Coolify open the **chatwoot-...** container,
Terminal, run `bundle exec rails c`, then (replace the ids with every existing account id and its owner user id):
```ruby
app = PlatformApp.find(2)
[[2, 2], [3, 3]].each do |acct, user|
  PlatformAppPermissible.find_or_create_by!(platform_app_id: app.id, permissible_type: "Account", permissible_id: acct)
  PlatformAppPermissible.find_or_create_by!(platform_app_id: app.id, permissible_type: "User", permissible_id: user)
end
```
Then `exit`. For the Citichoice profile add its account id and owner user id the same way (find them in Chatwoot Super Admin).

### A7. Scheduled tasks (Coolify > hub app > Scheduled Tasks)
| Command | Frequency |
|---|---|
| `python -m app.cli fx-sync` | daily |
| `python -m app.cli meta-billing-sync` | hourly |
| `python -m app.cli notify-run` | hourly |

### A8. WiseCP (once)
1. Upload `coremio/modules/Servers/DeskHub/` and delete `coremio/modules/Product/DeskHub/`.
2. Admin > Servers > Add Server (Citichoice Desk): address `hub.citichoice.net`, port 443, verify SSL on, username `hub`, Access Hash = the **admin** key from
   `python -m app.cli create-key wisecp-module --scope admin --ip <WiseCP server IP> --days 365`, hostname `hub.citichoice.net`, inbox URL, top-up add-on ID `48`.
3. Test Connection must succeed. (Cloudflare skip rule for the WiseCP IP on `hub.citichoice.net/admin/*` if it is blocked.)
4. Product (Hosting type) bound to the server, plan chosen from the list. WiseCP API key for the hub needs only: `GET /admin/invoices/{id}`, `GET /admin/financial/currencies`, `GET /admin/financial/currency-settings`.

## PART B: an EXISTING client (Bennyboom, Kynetiq Studios, Citichoice)
Gather first (write them down per client):
- Chatwoot **account id** and **owner user id** (Super Admin > Accounts / Users).
- Meta: **WABA id**, WhatsApp **Phone number ID**, and the **display phone number** (WhatsApp Manager > Phone numbers).
- WiseCP client; for Citichoice itself create a WiseCP client "Citichoice" so it has a service id (price override 0).

Then, per client:
1. **WiseCP order, Pending Approval** (Create Order, status Pending Approval, notifications off, invoice off or Paid). Open it; the number after `id=` in the service URL is `SERVICE_ID`.
2. **Platform access**: A6 for this client's account and owner user.
3. **Create the tenant**:
   `python -m app.cli create-tenant SERVICE_ID --name "Business Name" --email owner@their.com --plan growth --currency NGN --chatwoot-account ACCOUNT_ID --chatwoot-user USER_ID --no-owner-login`
   Wait about a minute, then `python -m app.cli list-tenants`: status must be `active`. If `provisioning_failed`: `python -m app.cli retry-job <job id>` after fixing what Audit says.
4. **Approve the WiseCP order.** The module adopts the tenant and does not touch the password. In WiseCP set the service Username to their real Chatwoot login email.
5. **Register the WhatsApp channel** (type the phone number by hand, with +):
   `python -m app.cli add-channel SLUG --type whatsapp --name "Name WhatsApp" --inbox INBOX_ID --phone +234XXXXXXXXXX --phone-number-id PHONE_NUMBER_ID --waba-id WABA_ID`
6. **Opening balance**: Desk Hub > tenant > add credit (or Adjustments).
7. **n8n key**: `python -m app.cli create-key n8n-SLUG --scope internal --tenant SLUG`. Copy the printed key. In n8n's `TENANTS` variable add to that client's entry:
   `"hub_url":"https://hub.citichoice.net","hub_key":"THE_KEY"`
8. **Import the workflow** as a new INACTIVE workflow; deactivate the old one; activate the new one; send one test message.
9. **Notifications**: `python -m app.cli set-notify SLUG --report on --low-balance on --whatsapp 234XXXXXXXXXX --email owner@their.com`
10. **Verify**: `python -m app.cli onboard-check SLUG` (everything OK), then `python -m app.cli meta-billing-sync --days 7 --dry-run`.
11. **Test a top-up** as that customer; check Audit for `topup.checked` with a `credited` result. If it fails, `python -m app.cli wisecp-invoice <invoice id>` shows how the hub reads the lines.

## PART C: a NEW client, from now on
1. Customer orders and pays in WiseCP. The module creates the tenant, a new Chatwoot account and the owner login automatically.
2. You: connect their WhatsApp number in Chatwoot (new inbox) and note the inbox id.
3. Do steps 5 to 11 of Part B for them (add-channel, opening balance if any, n8n key and TENANTS entry, workflow import, set-notify, onboard-check, top-up test).

## What is charged, and as what
- A bot reply on WhatsApp: **service** message, charged at send time.
- A template your workflows send: charged as the category the workflow declares (utility or marketing). Meta's daily numbers correct small mismatches between categories.
- Anything agents type or Campaigns send: charged by the daily Meta job.

## Note on variables
`docker-compose.yml` passes every setting to the hub container by name. If you add a new setting to the code, add it there too, or the container never sees it.
The workflow files and a ready-to-paste variable list are in `ENV-copy-paste.md`.
