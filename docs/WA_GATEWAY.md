# WhatsApp credit gateway: implementation guide

Goal: when a tenant's WhatsApp credit is empty, **no WhatsApp message leaves Chatwoot** (agent, bot, campaign, template), so Meta never
sees it and never bills it, and no typing indicator is shown to the customer.

## 1. What was wrong

| Symptom | Cause |
|---|---|
| Wallet goes negative | Only the n8n bot asked the hub before sending. Agent replies, templates and campaigns went Chatwoot -> Meta unchecked and were charged only by the daily Meta true-up (36 h lag), arriving later as a receivable. |
| Customer sees "typing" with no reply | n8n "Build transcript" sent the typing indicator and read receipt before "Ask AI" checked credit. |

## 2. The design

```
Customer --(inbound webhook, unchanged)--> Chatwoot
Agent / bot / campaign -> Chatwoot sidekiq --> HUB /wa/<secret>/v23.0/<phone_id>/messages --> Meta
                                                 |  1. phone_number_id -> tenant (tenant_channels.external_id)
                                                 |  2. price it, atomic hold on wallet
                                                 |  3. no funds  -> HTTP 402, Meta never contacted, Chatwoot shows "failed: credit is empty"
                                                 |  4. funds     -> forward; Meta accepts -> hold becomes the charge NOW
                                                 |               Meta rejects / times out -> hold released
```
* Read receipts, typing indicators, media, template sync: forwarded untouched, free.
* Templates are charged at their real Meta category (looked up with the tenant's own token, cached 1 h). If the lookup fails the send is
  still gated (at `WA_GATEWAY_TEMPLATE_CATEGORY`) but **not charged at send**; the daily true-up charges it at the right rate, so it can
  never be charged twice.
* The daily Meta true-up keeps working: it counts what the gateway already charged and only adds the difference.
* Suspended tenants are blocked. Numbers no tenant owns are forwarded (`WA_GATEWAY_UNMAPPED=allow`) or refused (`block`).

## 3. Files in this package

| File | Replace in |
|---|---|
| `desk-hub/` (whole project) | the hub repo. New: `app/services/wa_gateway.py`, `app/routers/wa_gateway.py`, `tests/test_wa_gateway.py`. Changed: `app/config.py`, `app/main.py`, `docker-compose.yml`, `.env.example` |
| `chatwoot/docker-compose.yml` | the Chatwoot stack in Coolify (adds `WHATSAPP_CLOUD_BASE_URL` to `chatwoot` and `sidekiq`; image tag via `CHATWOOT_VERSION`) |
| `chat-ai-replies-hub-billed.json` | each tenant's n8n "Chat AI replies" workflow (re-import, keep your Prompt text) |
| `DeskHub/` | the WiseCP module (dashboard card order) |

## 4. Rollout (do it in this order; each phase is safe on its own)

### Phase 0: before anything
1. Pin Chatwoot: in Coolify set `CHATWOOT_VERSION` to the tag you run today (check the running container's version in Chatwoot Settings).
2. Confirm your Chatwoot supports the setting. In the **sidekiq** container: `grep -rn WHATSAPP_CLOUD_BASE_URL /app/app/services/whatsapp`.
   No output = upgrade Chatwoot first. Do not continue until this prints a match.
3. Confirm each WhatsApp channel is registered in the hub with its phone_number_id and WABA id: `GET /internal/channels` (or the staff UI).
   BennyBoom `1189877034212184`, Kynetiq `1437668416089029`. `external_id` = phone_number_id, `external_account_id` = WABA id
   (the WABA id is needed for the template category lookup).

### Phase 1: fix the typing indicator (n8n only, no downtime)
Import `chat-ai-replies-hub-billed.json` into each tenant workflow. Leave `hub_gateway` = `false`. From now on a customer message at zero
credit shows no typing and no read receipt. Needs `hub_url` + `hub_key` in that tenant's TENANTS entry (already there).

### Phase 2: deploy the hub with the gateway OFF
1. Replace the hub project with `desk-hub/`; in Coolify add `WA_GATEWAY_SECRET` (generate: `openssl rand -hex 24`).
   Optional: `WA_GATEWAY_UNMAPPED=allow`, `WA_GATEWAY_TEMPLATE_CATEGORY=marketing` (these are the defaults).
2. Deploy. Migrations run automatically; this release adds none.
3. Test the hub directly with a tenant that has zero credit:
   ```
   curl -i -X POST "https://hub.citichoice.net/wa/<SECRET>/v23.0/<PHONE_ID>/messages" \
     -H "Authorization: Bearer x" -H "Content-Type: application/json" \
     -d '{"messaging_product":"whatsapp","to":"2348012345678","type":"text","text":{"body":"test"}}'
   ```
   Expected: `402` and `"WhatsApp credit is empty..."`. Wrong secret: `404`. Check Meta's analytics: nothing was sent.
   (A funded tenant would really send this message to that number, so test the funded case through Chatwoot with your own number.)

### Phase 3: switch Chatwoot to the gateway
1. Replace the Chatwoot compose with `chatwoot/docker-compose.yml`.
2. Coolify env: `WHATSAPP_CLOUD_BASE_URL=https://hub.citichoice.net/wa/<SECRET>` (no trailing slash, same secret as the hub).
3. Redeploy both `chatwoot` and `sidekiq`. Verify: `docker exec <sidekiq-container> env | grep WHATSAPP_CLOUD`.
4. Tests, in Chatwoot, to your own phone:
   * tenant at zero credit, agent replies -> message fails with the credit error, nothing in Meta analytics;
   * top up a small amount, reply -> delivered, balance drops immediately, no receivable;
   * keep sending until the balance can't cover one more -> the next send fails, balance never negative;
   * send an approved template -> usage row has the template's real category (staff UI usage list).
5. Only then set `hub_gateway` = `true` in each tenant's n8n Settings node (otherwise bot replies are charged twice).

Staged option: register channels in the hub one tenant at a time. Unregistered numbers pass through ungated, so you can enable
enforcement tenant by tenant even though Chatwoot's setting is instance-wide.

### Rollback (about 1 minute)
Clear `WHATSAPP_CLOUD_BASE_URL` (the compose default is Meta) and redeploy `chatwoot` + `sidekiq`; set `hub_gateway` back to `false`.
Sending goes direct again. Charges already made stay valid and the true-up still reconciles them.

## 5. Operating it
* **The hub is now in the sending path.** If it is down, Chatwoot cannot send WhatsApp (messages show failed). Add an uptime monitor on
  `https://hub.citichoice.net/healthz` and keep `restart: unless-stopped`. Expect about +50-150 ms per send.
* **Logs:** blocked sends log `{"event": "wa_send_blocked", "tenant": ..., "reason": ...}`. Reasons: `insufficient_funds`, `credit_limit_reached`,
  `tenant_suspended`, `unmapped`, pricing errors.
* **Customers:** agents see the failure reason inline in Chatwoot. Surface low-credit warnings through your existing low-balance alerts.
* **Inbound** messages are never affected.
* **Out of scope:** the Citichoice notification number (`NOTIFY_WA_*`) and n8n team alerts call Meta directly and keep their own hub checks.

## 6. Known limits
* Two sends at the same instant can't both pass on one message's worth of credit (atomic hold), but a Meta-side failure after Meta accepted
  (timeout) is released and reconciled by the daily true-up.
* The marketing minimum-balance floor is not applied to templates here; they are gated on capacity only.
* Dial-code to country mapping covers common markets; unknown prefixes use the default `*` rate.
* The secret is part of Chatwoot's configured URL. Rotate by changing it in both places and redeploying hub, chatwoot and sidekiq together.
* Pre-existing: 3 hub tests assume `SERVICE_MESSAGES_FREE=true` and fail with the default; they are unrelated to this change.
