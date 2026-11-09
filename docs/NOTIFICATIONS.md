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
