# Customer authentication: what is known

## Inspected
- A web search for `account.citichoice.co` returned nothing about it (only unrelated bank sites).
- The sandbox cannot reach your network, so no discovery document, JWKS or login page was inspected.
- Only your own project notes exist: a self-hosted OIDC/OAuth2 hub, Logto preferred with Authentik as the alternative, already used for WordPress, WHMCS and Magento at the protocol level.

## Therefore NOT verified
Whether the IdP is deployed, its issuer URL, the audience/client the hub should use, which claims it issues (`sub`, `email`, `email_verified`), the token type (JWT or opaque), and how sessions reach a separate dashboard.

## What exists in the hub
- `app/auth/identity.py`: an `IdentityProvider` interface and an `Identity(subject, email, email_verified)` value.
- A development/test-only header adapter. In production the customer API answers 503 `sso_not_configured`. Tested.
- `tenant_members`: the server-side record of which IdP subject may see which tenant. `/customer/tenants` and `/customer/tenants/{id}` derive access from it and ignore any tenant id the browser supplies. A non-member gets the same 404 as a missing tenant. Customer responses omit markup, Chatwoot ids and WiseCP ids.
- First sign-in links an invitation to a subject only when the IdP says the email is verified.

## To finish (needs the real IdP)
1. Confirm issuer, discovery URL and which client/audience the hub validates.
2. Write an adapter that verifies signature (JWKS), `iss`, `aud`, `exp`/`nbf`, and requires `email_verified` before linking an invite.
3. Decide token transport (Bearer from the dashboard backend, or cookie session) and CORS accordingly.
4. Keep service credentials (admin/internal keys) separate; they are never accepted on `/customer`.
