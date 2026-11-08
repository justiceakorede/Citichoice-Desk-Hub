"""Chatwoot Platform API client. [Check] each endpoint against your Chatwoot version.

Needs a Platform App token (Super Admin > Platform Apps). A Platform App can only manage
accounts and users it created, so adopt existing accounts by creating them through this app
or by granting the app access in the Super Admin console.
"""
import secrets

import httpx


class ProvisioningError(Exception):
    """ambiguous=True means the request may have reached Chatwoot and succeeded (timeout, 5xx).
    A blind retry of a create call could then make a duplicate, so callers must not retry creates."""
    def __init__(self, message: str, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


class ChatwootClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.h = {"api_access_token": token}
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw):
        if not self.base:
            raise ProvisioningError("CHATWOOT_URL is not configured")
        try:
            r = httpx.request(method, f"{self.base}/platform/api/v1{path}", headers=self.h,
                              timeout=self.timeout, **kw)
        except httpx.ConnectError as e:      # never reached the server: safe to retry
            raise ProvisioningError(f"Chatwoot unreachable: {type(e).__name__}", ambiguous=False) from e
        except httpx.HTTPError as e:         # timeout or mid-flight failure: outcome unknown
            raise ProvisioningError(f"Chatwoot request failed: {type(e).__name__}", ambiguous=True) from e
        if r.status_code >= 400:
            raise ProvisioningError(f"Chatwoot {method} {path} -> {r.status_code}: {r.text[:200]}",
                                    ambiguous=r.status_code >= 500)
        return r.json() if r.content else {}

    def create_account(self, name: str, limits: dict | None = None) -> int:
        body = {"name": name}
        if limits:
            body["limits"] = limits
        return int(self._req("POST", "/accounts", json=body)["id"])

    def update_account(self, account_id: int, **fields) -> None:
        self._req("PATCH", f"/accounts/{account_id}", json=fields)

    def set_status(self, account_id: int, status: str) -> None:   # "active" | "suspended"
        self.update_account(account_id, status=status)

    def delete_account(self, account_id: int) -> None:
        self._req("DELETE", f"/accounts/{account_id}")

    def create_user(self, name: str, email: str) -> int:
        # Random password nobody sees. The customer signs in via the one-time login link
        # and then sets their own password. No password is ever emailed.
        pw = secrets.token_urlsafe(24) + "aA1!"
        return int(self._req("POST", "/users", json={"name": name, "email": email, "password": pw})["id"])

    def add_user(self, account_id: int, user_id: int, role: str = "administrator") -> None:
        self._req("POST", f"/accounts/{account_id}/account_users", json={"user_id": user_id, "role": role})

    def login_link(self, user_id: int) -> str:
        return self._req("GET", f"/users/{user_id}/login")["url"]

    def set_user_password(self, user_id: int, password: str) -> None:
        """[Check] Platform API `PATCH /users/{id}` accepts `password`. Used so WiseCP can show a password next to the login."""
        self._req("PATCH", f"/users/{user_id}", json={"password": password})

    def user_token(self, user_id: int) -> str:
        """[Check] Platform API `GET /users/{id}` returns the user's access_token for users the Platform App created.
        Lets the hub read an account's reports as its own owner, with no extra seat and no extra user to remove."""
        return str(self._req("GET", f"/users/{user_id}").get("access_token") or "")
