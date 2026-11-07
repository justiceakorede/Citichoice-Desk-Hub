# Zitadel live checklist

The automated staff-login tests use a MOCK identity provider. They prove the hub validates what it should; they do
NOT prove it works with your real Zitadel. Do this once against `https://accounts.citichoice.co`, on a staging or
production deployment of `deskhub.citichoice.net`, and record the result in docs/IMPLEMENTATION_STATUS.md.

## Zitadel side (project "Citichoice Desk Hub", ID 396543381326529539)
1. Project settings ON: return roles during authentication; only authorized users can authenticate; restrict to granted organizations.
2. Role `administrator` exists. Grant it to ONE test user (A). Create a second user (B) with no grant.
3. Application "Citichoice Desk Hub": Web, Authentication method CODE (Basic). Redirect `https://deskhub.citichoice.net/auth/callback`,
   post-logout `https://deskhub.citichoice.net/auth/logged-out`. Development mode OFF.
4. Application token settings: roles inside ID token ON, user info inside ID token ON.
5. Client secret goes into Coolify as `OIDC_CLIENT_SECRET` (never into chat, git or logs).

## Hub side (Coolify environment)
`ENV=production STAFF_UI_HOST=deskhub.citichoice.net API_HOST=hub.citichoice.net OIDC_ISSUER=https://accounts.citichoice.co`
`OIDC_CLIENT_ID=396642667028612099 OIDC_REDIRECT_URI=... OIDC_POST_LOGOUT_URI=... OIDC_PROJECT_ID=396543381326529539 OIDC_REQUIRED_ROLE=administrator`
The app refuses to start in production if any of these is missing or inconsistent.

## Tests (tick each; note anything unexpected)
- [ ] `GET https://deskhub.citichoice.net/ui` signed out redirects to `/auth/login`, then to accounts.citichoice.co.
- [ ] User A signs in and lands on `/ui`; the tenant list loads. Logs show `staff login` without any token.
- [ ] **Role claim:** if A is refused, read the log line "role claim names present" (names only). Zitadel emits
      `urn:zitadel:iam:org:project:<projectId>:roles` and/or `urn:zitadel:iam:org:project:roles`; the hub accepts both. If
      neither appears, enable the token settings in step 4 or set `OIDC_ROLE_CLAIM` to the claim you see.
- [ ] User B (no grant) is refused by Zitadel itself at the login screen.
- [ ] User C: has a Zitadel login and a grant in the DASHBOARD project only. Must be refused by Zitadel or by the hub (403 page, no session).
- [ ] `POST /ui/...` actions fail without the CSRF token (test with browser dev tools: remove the hidden field).
- [ ] Sign out ends the session at the hub and at Zitadel; the back button then redirects to sign-in.
- [ ] Remove A's `administrator` grant. Existing session stays valid until it expires (max 30 min idle / 8 h absolute); a NEW sign-in is refused.
      (To cut this window, shorten SESSION_IDLE_MINUTES; back-channel logout is supported by your Zitadel and can be added later.)
- [ ] `https://hub.citichoice.net/ui` and `https://deskhub.citichoice.net/admin/tenants` both return 404 (host separation).
- [ ] Cloudflare Access is in front of `deskhub.citichoice.net` ONLY. `accounts.citichoice.co` stays DNS-only (the hub server calls it directly).
