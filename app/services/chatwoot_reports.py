"""Chatwoot account reports (conversation and message counts). [Check] against your Chatwoot version.

Uses `GET /api/v2/accounts/{account_id}/reports/summary?type=account|inbox&id=&since=&until=` with an access token of a
user that belongs to the account. The token is the hub's "reporter" user (CHATWOOT_REPORT_TOKEN). The Platform App token
used for provisioning cannot read account reports, so this is a separate, read-only credential.
"""
import datetime as dt

import httpx


class ReportError(Exception):
    pass


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class ChatwootReports:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base and self.token)

    def summary(self, account_id: int, since: dt.datetime, until: dt.datetime, inbox_id: int | None = None) -> dict:
        if not self.configured:
            raise ReportError("not_configured")
        params = {"since": str(int(since.timestamp())), "until": str(int(until.timestamp())),
                  "type": "inbox" if inbox_id else "account"}
        if inbox_id:
            params["id"] = str(inbox_id)
        try:
            r = httpx.get(f"{self.base}/api/v2/accounts/{account_id}/reports/summary", params=params,
                          headers={"api_access_token": self.token}, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ReportError(f"unreachable:{type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise ReportError("reporter_not_in_account")      # the reporter user is not an administrator of this account
        if r.status_code >= 400:
            raise ReportError(f"http_{r.status_code}")
        try:
            d = r.json()
        except ValueError as e:
            raise ReportError("bad_payload") from e
        if not isinstance(d, dict):
            raise ReportError("bad_payload")
        inc, out = _int(d.get("incoming_messages_count")), _int(d.get("outgoing_messages_count"))
        return {"conversations": _int(d.get("conversations_count")), "incoming_messages": inc, "outgoing_messages": out,
                "messages": inc + out, "resolutions": _int(d.get("resolutions_count"))}


    def inboxes(self, account_id: int) -> list[dict]:
        """The account's inboxes, as Chatwoot knows them. [Check] `GET /api/v1/accounts/{id}/inboxes` and its channel_type values."""
        if not self.configured:
            raise ReportError("not_configured")
        try:
            r = httpx.get(f"{self.base}/api/v1/accounts/{account_id}/inboxes", headers={"api_access_token": self.token}, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ReportError(f"unreachable:{type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise ReportError("reporter_not_in_account")
        if r.status_code >= 400:
            raise ReportError(f"http_{r.status_code}")
        try:
            rows = r.json().get("payload", [])
        except (ValueError, AttributeError) as e:
            raise ReportError("bad_payload") from e
        out = []
        for i in rows if isinstance(rows, list) else []:
            if isinstance(i, dict) and i.get("id"):
                out.append({"id": int(i["id"]), "name": str(i.get("name") or "Inbox"), "type": _TYPE.get(str(i.get("channel_type", "")), "other"),
                            "phone_number": i.get("phone_number") or None})
        return out


_TYPE = {"Channel::Whatsapp": "whatsapp", "Channel::FacebookPage": "messenger", "Channel::Instagram": "instagram",
         "Channel::Telegram": "telegram", "Channel::Email": "email", "Channel::WebWidget": "web_chat", "Channel::TiktokChannel": "tiktok",
         "Channel::Tiktok": "tiktok"}
