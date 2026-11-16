# Weekly reports and low-balance alerts (NOT RUN)

Sent from YOUR WhatsApp number and by SMTP email. Each customer has switches on their tenant page in Desk Hub.

## One-time setup
1. In your own WhatsApp Business account create two **Utility** templates (English), exactly these names:
   - `weekly_report`: `Hi {{1}}, your Citichoice Desk WhatsApp summary for {{2}}: {{3}} messages sent, total cost {{4}}. Credit left: {{5}}.`
   - `low_balance_alert`: `Hi {{1}}, your Citichoice Desk WhatsApp credit is {{2}}. {{3}}`
   Meta must approve both. Sample values for the review: Bennyboom, 03 Nov - 09 Nov 2026, 1,240, NGN 12,300.00, NGN 7,700.00.
2. Coolify env: `NOTIFY_WA_PHONE_NUMBER_ID` (your number's id), `NOTIFY_WA_TOKEN` (or it reuses `META_ACCESS_TOKEN`), and for email
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`.
3. Deploy (migrations 0011 and 0012 must run).
4. Per customer, in Desk Hub tenant page > Customer notifications (or CLI):
   `python -m app.cli set-notify bennyboom-support --report on --low-balance on --whatsapp 2348012345678 --email owner@x.com`
   Only add numbers and emails that agreed to receive these.
5. Coolify scheduled task, hourly: `python -m app.cli notify-run`. Test first with `--dry-run`, or `--force-report` for one tenant's report now.

## Behaviour
- The report covers the last complete Monday-Sunday week (Lagos time), sent Monday from 08:00, once per week.
- The low-balance alert is sent when the level reaches low, critical, zero or owing, again only if it gets worse or after 72 hours, and resets when topped up.
- A WhatsApp that Meta rejects (for example the customer never messaged you and the template is not approved) is reported in the command output and the audit trail.

## Seeing what was sent
Desk Hub > Notifications lists every weekly report and low-balance alert by customer, kind, channel (WhatsApp or email), recipient and result
(sent or failed, with the reason). Each tenant page shows its last 8. Migration 0013 creates the table; entries appear from the next `notify-run`.

## How often, and on demand
Each customer has a frequency per channel (tenant page > Customer notifications, or `set-notify`):
- **Off, Daily, Weekly or Monthly**, separately for WhatsApp and email, so a client can have daily email and weekly WhatsApp.
- **Send time** (Lagos hour) and, for weekly, the **weekday**.
- Daily covers yesterday, weekly the 7 whole days before the send day, monthly the previous calendar month. A channel is sent once per period.
  If the hub was down at the scheduled time, it still sends within the next 24 hours; later than that it waits for the next period.
- Customers set up before this existed stay on weekly Monday 08:00 for both channels.

**Send a report now** (tenant page, or `python -m app.cli send-report <slug> --period last7 --only whatsapp`): pick yesterday, last 7 or 30 days, this month so far,
or custom dates; WhatsApp, email or both; optionally one extra number or email just this once. It does not change the schedule, and it appears on the
Notifications page as "Sent on demand" with who triggered it.

## Low-balance alerts: your level and your interval
Per customer (tenant page, or `set-notify --low-threshold-usd 10 --low-repeat-hours 24`):
- **Alert when credit is below X USD** (converted at today's rate; credit means what is available minus anything owed). 0 keeps the automatic levels
  (empty, owing, or under 20% of the last 30 days' spend).
- **Remind every N hours** while it stays below; 0 sends once per dip. When the credit is back above the level the alert re-arms.
Nothing is sent unless the customer's **Low-balance alert** box is ticked and a WhatsApp number or email is saved.

If an alert you expected did not go out, run `python -m app.cli notify-run --dry-run`. It prints a `skipped` line per customer with the reason
(switched off, no recipients, above the level, already alerted and when the next reminder is due, exchange rate missing). Each real run also writes a
`notify.run` entry to the Audit page, so you can see whether the hourly task is running at all.

## Editing the wording
Desk Hub > Templates. Placeholders are written `{{business}}`, `{{balance}}`, `{{topup_link}}` and so on (the page lists them all). Email is sent as HTML plus a plain-text copy.
For WhatsApp the wording is fixed by Meta's approved template, so you choose the template name and what goes in each numbered variable; keep the "approved wording" box
the same as Meta's so previews and the log match. Preview shows both with sample figures, and Send test sends the saved version to an address of yours.
Set `NOTIFY_TOPUP_URL` (your client-area link) so the Buy credit button and `{{topup_link}}` work.

## Seeing exactly what was sent
Notifications > View shows the real email (subject, HTML and plain text) or the WhatsApp template, its variables and the resulting wording, exactly as sent.
Entries from before this was added show no content. Migration 0014 is required.
