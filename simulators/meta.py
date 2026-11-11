"""Meta WhatsApp Cloud API, simulated (the Graph API + webhooks), for ``whatsapp_provider="meta"``.

Shares phones, media and the async dispatcher with the `ChannelSimulator` hub:
a phone can write on WhatsApp through Meta and call through Twilio Voice, like
a real deployment that uses Meta for WhatsApp and Twilio for the phone line.

What it reproduces:

* Webhooks: ``{"object": "whatsapp_business_account", "entry": [...]}`` with
  ``messages`` (text, audio voice notes, images, button replies...) and
  ``statuses``; ``X-Hub-Signature-256`` = HMAC-SHA256(app secret, raw body);
  the ``GET`` subscription handshake (``hub.mode`` / ``hub.verify_token`` /
  ``hub.challenge``).
* Graph API: ``POST /{version}/{phone-number-id}/messages`` (text, template,
  ``status: read``), ``GET /{version}/{media-id}`` and the media download,
  bearer auth, Meta's error envelope and codes (190, 100, 131030, 132001, 2).
* Delivery: statuses ``sent`` → ``delivered`` → ``read`` are posted back
  asynchronously; a free-form message outside the 24h window is accepted and
  then **fails with 131047** in a status webhook, as on the real platform.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional

from .core import LogEntry, WhatsAppMessage, e164

if TYPE_CHECKING:
    from .core import ChannelSimulator, Media, Phone

DEFAULT_META_TOKEN = "EAAG-simulated-access-token"
DEFAULT_APP_SECRET = "simulated-app-secret"
DEFAULT_VERIFY_TOKEN = "simulated-verify-token"
DEFAULT_PHONE_NUMBER_ID = "106540352242922"
DEFAULT_WABA_ID = "102290129340398"
GRAPH_VERSION = "v21.0"

WINDOW_ERROR = {
    "code": 131047, "title": "Re-engagement message", "message": "Re-engagement message",
    "error_data": {"details": "Message failed to send because more than 24 hours have passed since the customer "
                              "last replied to this number."},
}


def wamid() -> str:
    return "wamid.HBgL" + secrets.token_hex(18).upper()


def graph_error(status: int, code: int, message: str, error_type: str = "OAuthException") -> tuple[int, dict]:
    return status, {"error": {"message": message, "type": error_type, "code": code,
                              "fbtrace_id": "A" + secrets.token_hex(10)}}


def digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


class MetaSim:
    """The Meta plugin: the Cloud API of one WhatsApp Business phone number."""

    def __init__(self, sim: "ChannelSimulator", *, access_token: str = DEFAULT_META_TOKEN,
                 app_secret: str = DEFAULT_APP_SECRET, verify_token: str = DEFAULT_VERIFY_TOKEN,
                 phone_number_id: str = DEFAULT_PHONE_NUMBER_ID, waba_id: str = DEFAULT_WABA_ID, sign: bool = True):
        self.sim = sim
        self.access_token = access_token
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id
        self.sign = sign
        self.templates: dict[str, str] = {}  # approved template name -> body with {{1}}, {{2}}
        self.sent: list[dict[str, Any]] = []  # every Graph API message the app sent (JSON bodies)

    # ---------- webhooks (simulator -> app) ----------

    @property
    def webhook_url(self) -> str:
        return self.sim.public_url(self.sim.paths.meta_webhook)

    def signature(self, raw: bytes) -> str:
        return "sha256=" + hmac.new(self.app_secret.encode(), raw, hashlib.sha256).hexdigest()

    def _envelope(self, value: dict[str, Any]) -> dict[str, Any]:
        return {"object": "whatsapp_business_account", "entry": [{"id": self.waba_id, "changes": [{
            "field": "messages",
            "value": {"messaging_product": "whatsapp",
                      "metadata": {"display_phone_number": digits(self.sim.whatsapp_number),
                                   "phone_number_id": self.phone_number_id},
                      **value},
        }]}]}

    def post(self, payload: dict[str, Any], phone: Optional[str] = None, kind: str = "webhook") -> bool:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "facebookexternalua"}
        if self.sign:
            headers["X-Hub-Signature-256"] = self.signature(raw)
        return self.sim._webhook(self.webhook_url, None, phone=phone, kind=kind, raw=raw, headers=headers,
                                 expect_twiml=False, log_params=payload) is not None

    def verify_webhook(self, token: Optional[str] = None) -> tuple[Optional[int], str]:
        """What Meta does when you save the webhook URL in the app dashboard."""
        challenge = str(secrets.randbelow(10**9))
        params = {"hub.mode": "subscribe", "hub.verify_token": token or self.verify_token, "hub.challenge": challenge}
        entry = LogEntry(kind="webhook", method="GET", url=self.webhook_url, params=params)
        started = time.monotonic()
        try:
            resp = self.sim.transport.request("GET", self.sim._transport_url(self.webhook_url), params=params)
        except Exception as exc:  # noqa: BLE001 - report anything as a failed verification
            entry.error = f"verification failed: {exc}"
            self.sim._log(entry, started, None)
            return None, ""
        entry.status, entry.response = resp.status_code, resp.text[:500]
        if resp.status_code != 200 or resp.text != challenge:
            entry.error = "webhook verification failed: the endpoint must echo hub.challenge"
        self.sim._log(entry, started, None)
        return resp.status_code, resp.text

    def inbound(self, phone: "Phone", message: WhatsAppMessage, media: Optional["Media"] = None) -> list[str]:
        raw: dict[str, Any] = {"from": digits(phone.number), "id": message.sid, "timestamp": str(int(time.time()))}
        if media is None:
            raw.update(type="text", text={"body": message.body})
        else:
            kind = media.whatsapp_type
            info: dict[str, Any] = {"mime_type": media.mime, "sha256": media.sha256, "id": media.id}
            if kind == "audio":
                info["voice"] = media.voice
            elif message.body:
                info["caption"] = message.body
            raw.update(type=kind, **{kind: info})
        payload = self._envelope({"contacts": [{"profile": {"name": phone.name or "Candidato"},
                                                "wa_id": digits(phone.number)}],
                                  "messages": [raw]})
        phone._last_inbound = payload
        return phone._exchange(lambda: self.post(payload, phone=phone.number))

    # ----- messaging plugin interface -----

    def new_message_id(self) -> str:
        return wamid()

    def resend(self, phone: "Phone", payload: dict[str, Any]) -> list[str]:
        return phone._exchange(lambda: self.post(payload, phone=phone.number))

    def receipt(self, phone: "Phone", message: WhatsAppMessage, status: str, code: Optional[int]) -> None:
        error = (WINDOW_ERROR if code == 131047 else {"code": code, "title": "Failed"}) if code else None
        self.status_webhook(phone, message, status, error)

    def status_webhook(self, phone: "Phone", message: WhatsAppMessage, status: str,
                       error: Optional[dict[str, Any]] = None) -> None:
        item: dict[str, Any] = {"id": message.sid, "status": status, "timestamp": str(int(time.time())),
                                "recipient_id": digits(phone.number)}
        if status in ("sent", "delivered"):
            item["conversation"] = {"id": "CONV" + secrets.token_hex(6), "origin": {"type": "service"}}
            item["pricing"] = {"billable": True, "pricing_model": "CBP", "category": "service"}
        if error:
            item["errors"] = [error]
        self.post(self._envelope({"statuses": [item]}), phone=phone.number, kind="status_callback")

    # ---------- Graph API (app -> simulator) ----------

    def handle_graph(self, method: str, path: str, body: Optional[dict[str, Any]],
                     authorization: str) -> tuple[int, dict[str, Any]]:
        entry = LogEntry(kind="rest", method=method.upper(), url=path, params=body or {})
        started = time.monotonic()
        status, response = self._route(method.upper(), path, body or {}, authorization)
        entry.status, entry.response = status, json.dumps(response, ensure_ascii=False)[:2000]
        entry.phone = response.pop("_phone", None)
        if status >= 400:
            error = response.get("error") or {}
            entry.error = f"{error.get('code')}: {error.get('message')}"
        self.sim._log(entry, started, None)
        return status, response

    def authorized(self, authorization: str) -> bool:
        token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        return bool(token) and secrets.compare_digest(token, self.access_token)

    def _route(self, method: str, path: str, body: dict[str, Any], authorization: str) -> tuple[int, dict]:
        if self.sim.faults.rest_down:
            return graph_error(503, 2, "Service temporarily unavailable")
        if not self.authorized(authorization):
            return graph_error(401, 190, "Invalid OAuth access token - Cannot parse access token")
        match = re.fullmatch(r"/v\d+\.\d+/([\w.]+)(/messages)?", path)
        if match is None:
            return graph_error(400, 100, f"Unknown path components: {path}")
        node, edge = match.groups()
        if edge:
            if method != "POST":
                return graph_error(400, 100, "Unsupported get request")
            if node != self.phone_number_id:
                return graph_error(400, 100, f"Unsupported post request. Object with ID '{node}' does not exist, "
                                             "cannot be loaded due to missing permissions, or does not support this "
                                             "operation", "GraphMethodException")
            return self._message(body)
        if method == "GET":
            media = self.sim.media.get(node)
            if media is None:
                return graph_error(400, 100, f"Unsupported get request. Object with ID '{node}' does not exist",
                                   "GraphMethodException")
            return 200, {"messaging_product": "whatsapp", "url": f"{self.sim.graph_url}/meta-media/{media.id}",
                         "mime_type": media.mime, "sha256": media.sha256, "file_size": len(media.data), "id": media.id}
        return graph_error(400, 100, "Unsupported request")

    def _message(self, body: dict[str, Any]) -> tuple[int, dict]:
        if body.get("messaging_product") != "whatsapp":
            return graph_error(400, 100, "(#100) The parameter messaging_product is required.")
        if body.get("status") == "read":
            self.sim.mark_read_by_business(str(body.get("message_id") or ""))
            return 200, {"success": True}
        to = digits(str(body.get("to") or ""))
        if not to:
            return graph_error(400, 100, "(#100) The parameter to is required.")
        allowed = self.sim.faults.recipient_allow_list
        if allowed and f"+{to}" not in {e164(n) for n in allowed}:
            return graph_error(400, 131030, "(#131030) Recipient phone number not in allowed list")
        kind = body.get("type") or "text"
        phone = self.sim.phone(f"+{to}")
        if kind == "text":
            text = str((body.get("text") or {}).get("body") or "")
            if not text:
                return graph_error(400, 100, "(#100) The parameter text['body'] is required.")
            window_closed = self.sim.faults.enforce_window and not phone.window_open()
            message = self.sim.deliver_whatsapp(phone, text, via="graph", provider="meta", sid=wamid(),
                                                failed_code=131047 if window_closed else None)
        elif kind == "template":
            template = body.get("template") or {}
            name = str(template.get("name") or "")
            if name not in self.templates:
                return graph_error(404, 132001, f"(#132001) Template name does not exist in the translation: {name}")
            params = [p.get("text", "") for c in template.get("components") or [] if c.get("type") == "body"
                      for p in c.get("parameters") or []]
            text = re.sub(r"\{\{(\d+)\}\}", lambda m: str(params[int(m.group(1)) - 1]) if int(m.group(1)) <= len(params)
                          else m.group(0), self.templates[name])
            message = self.sim.deliver_whatsapp(phone, text, via="template", provider="meta", sid=wamid(),
                                                content_sid=name)
        else:
            return graph_error(400, 100, f"(#100) Param type must be one of {{text, template}} here, got {kind}")
        self.sent.append({**body, "_id": message.sid})
        response = {"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
                    "messages": [{"id": message.sid}], "_phone": phone.number}
        if kind == "template":
            response["messages"][0]["message_status"] = "accepted"
        return 200, response
