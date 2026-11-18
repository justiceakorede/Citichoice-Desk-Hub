"""Meta Cloud API gateway for Chatwoot. Set Chatwoot's WHATSAPP_CLOUD_BASE_URL to https://<api host>/wa/<WA_GATEWAY_SECRET>.
Everything except message sends is forwarded untouched (media, templates, read receipts, typing)."""
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..services import wa_gateway as gw

router = APIRouter(prefix="/wa")
log = logging.getLogger("hub.wa_gateway")
_PASS_HEADERS = ("authorization", "content-type", "accept")


def _meta_error(status: int, message: str, code: int = 131049) -> Response:
    body = {"error": {"message": message, "type": "CreditError", "code": code, "fbtrace_id": "hub-gateway"}}
    return Response(json.dumps(body), status_code=status, media_type="application/json")


def _forward(request: Request, path: str, raw: bytes) -> httpx.Response:
    s = get_settings()
    headers = {k: v for k, v in request.headers.items() if k.lower() in _PASS_HEADERS}
    return httpx.request(request.method, f"{s.wa_gateway_upstream.rstrip('/')}/{path}", params=dict(request.query_params),
                         content=raw, headers=headers, timeout=s.wa_gateway_timeout)


def _relay(r: httpx.Response) -> Response:
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))


@router.api_route("/{secret}/{path:path}", methods=["GET", "POST", "DELETE"])
async def gateway(secret: str, path: str, request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    if not s.wa_gateway_secret or not hmac.compare_digest(secret, s.wa_gateway_secret):
        return Response('{"detail":"not found"}', status_code=404, media_type="application/json")
    raw = await request.body()
    return await run_in_threadpool(_handle, request, path, raw, db)      # blocking DB and HTTP work stays off the event loop


def _handle(request: Request, path: str, raw: bytes, db: Session) -> Response:
    m = gw.SEND_PATH.match(path) if request.method == "POST" else None
    body = None
    if m:
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
    if not (m and gw.is_message_send(body)):
        try:
            return _relay(_forward(request, path, raw))
        except httpx.HTTPError as e:
            return _meta_error(504, f"WhatsApp upstream unreachable ({type(e).__name__})", 2)

    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip() or None
    d = gw.authorize(db, m.group(1), body, token)
    if not d.allow:
        log.info(json.dumps({"event": "wa_send_blocked", "tenant": d.tenant_id, "reason": d.reason}))
        return _meta_error(402, d.message or "WhatsApp sending is not allowed.")
    try:
        r = _forward(request, path, raw)
    except httpx.HTTPError as e:
        gw.release(db, d)                         # outcome unknown: the daily Meta true-up charges it if Meta did bill
        return _meta_error(504, f"WhatsApp upstream unreachable ({type(e).__name__})", 2)
    wamid = None
    if r.status_code < 400:
        try:
            wamid = (r.json().get("messages") or [{}])[0].get("id")
        except ValueError:
            wamid = None
    try:
        gw.settle(db, d, wamid) if wamid else gw.release(db, d)
    except Exception:                             # never hide Meta's answer from Chatwoot; the true-up covers it
        log.exception("wa gateway settle failed")
        db.rollback()
        try:
            gw.release(db, d)
        except Exception:
            db.rollback()
    return _relay(r)
