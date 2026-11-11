"""Our client for Meta's WhatsApp Cloud API (Graph API calls + webhook payload parsing).

Differences with Twilio that shape the integration:

* Webhooks are JSON, signed with ``X-Hub-Signature-256`` (HMAC-SHA256 of the
  raw body with the app secret), and subscribing requires a ``GET``
  handshake that echoes ``hub.challenge`` when ``hub.verify_token`` matches.
* A webhook cannot carry the reply: every answer is a Graph API call
  (``POST /{version}/{phone-number-id}/messages``).
* Delivery is reported later, in ``statuses`` webhooks. A free-form message
  outside the 24h customer-service window is **accepted** and then fails with
  error 131047 in a status webhook; `MessagingChannel.on_status` falls back to
  the template then.
* Media is two steps: ``GET /{media-id}`` gives a short-lived URL, which is
  downloaded with the same bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Mapping, Optional

import httpx

from ...channels.contracts import InboundMessage, MediaError, MediaRef, SendResult, StatusUpdate
from ...config import settings

log = logging.getLogger("sazon.meta")

# 131047: more than 24h since the customer last wrote (re-engagement needs a template).
WINDOW_ERROR_CODES = frozenset({131047})
_MEDIA_TYPES = ("audio", "image", "video", "document", "sticker")


class MetaClient:
    name = "meta"
    kind = "whatsapp"  # the channel it carries
    replies_in_webhook = False  # every reply is a Graph API call
    window_error_codes = WINDOW_ERROR_CODES
    template_setting = "META_TEMPLATE_NAME"

    def __init__(self, config: Any = settings, http: Optional[Any] = None):
        self._config = config
        self._http = http  # httpx.Client-compatible; tests plug the simulator in here

    # ---------- configuration ----------

    @property
    def configured(self) -> bool:
        return bool(self._config.meta_access_token and self._config.meta_phone_number_id)

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.Client(timeout=10)
        return self._http

    def _url(self, path: str) -> str:
        return f"{self._config.meta_graph_base_url}/{self._config.meta_graph_version}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.meta_access_token}"}

    # ---------- webhook security ----------

    def verify_subscription(self, mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Optional[str]:
        """The GET handshake Meta sends when the webhook is configured. Returns the challenge to echo."""
        expected = self._config.meta_verify_token
        if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
            return challenge or ""
        return None

    def validate_signature(self, raw_body: bytes, header: str) -> bool:
        if not self._config.meta_validate_signatures:
            return True
        secret = self._config.meta_app_secret
        if not secret:
            log.warning("META_APP_SECRET is empty: rejecting webhook (set it or META_VALIDATE_SIGNATURES=false)")
            return False
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (header or "").strip())

    # ---------- inbound ----------

    def parse_webhook(self, payload: Mapping[str, Any]) -> tuple[list[InboundMessage], list[StatusUpdate]]:
        """Messages and delivery statuses for *our* phone number in a webhook payload."""
        messages: list[InboundMessage] = []
        statuses: list[StatusUpdate] = []
        if payload.get("object") != "whatsapp_business_account":
            return messages, statuses
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                number_id = (value.get("metadata") or {}).get("phone_number_id")
                if number_id and self._config.meta_phone_number_id and number_id != self._config.meta_phone_number_id:
                    continue  # another number of the same WhatsApp Business Account
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for raw in value.get("messages") or []:
                    message = self._message(raw, names)
                    if message is not None:
                        messages.append(message)
                for raw in value.get("statuses") or []:
                    error = (raw.get("errors") or [{}])[0]
                    statuses.append(StatusUpdate(
                        provider=self.name, external_id=str(raw.get("id") or ""), status=str(raw.get("status") or ""),
                        error_code=_int_or_none(error.get("code")),
                        error_detail=(error.get("error_data") or {}).get("details") or error.get("title"),
                        contact=_e164(raw.get("recipient_id")),
                    ))
        return messages, statuses

    def _message(self, raw: Mapping[str, Any], names: Mapping[str, Any]) -> Optional[InboundMessage]:
        kind = raw.get("type") or "unsupported"
        sender = str(raw.get("from") or "")
        message = InboundMessage(contact=_e164(sender) or "", message_id=raw.get("id"), provider=self.name,
                                 profile_name=names.get(sender))
        if kind == "text":
            message.text = str((raw.get("text") or {}).get("body") or "")
        elif kind == "button":  # quick reply of a template ("Sí, seguimos")
            message.text = str((raw.get("button") or {}).get("text") or "")
        elif kind == "interactive":
            interactive = raw.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            message.text = str(reply.get("title") or "")
        elif kind in _MEDIA_TYPES:
            media = raw.get(kind) or {}
            message.media.append(MediaRef(mime_type=str(media.get("mime_type") or f"{kind}/unknown"),
                                          id=media.get("id"), voice=bool(media.get("voice"))))
            message.text = str(media.get("caption") or "")
            if kind == "sticker":
                message.kind = "sticker"
        elif kind == "reaction":
            return None  # an emoji on one of our messages: nothing to answer
        else:  # location, contacts, unsupported, ...
            message.kind = kind
        return message

    # ---------- outbound ----------

    def _send(self, payload: dict[str, Any]) -> SendResult:
        if not self.configured:
            log.warning("whatsapp send skipped: META_ACCESS_TOKEN / META_PHONE_NUMBER_ID not configured")
            return SendResult(ok=False, error="not configured")
        try:
            resp = self.http.post(self._url(f"{self._config.meta_phone_number_id}/messages"),
                                  headers=self._headers(), json={"messaging_product": "whatsapp", **payload})
        except httpx.HTTPError as exc:
            log.error("Graph API unreachable: %s", exc)
            return SendResult(ok=False, error=str(exc))
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or "error" in body:
            error = body.get("error") or {}
            code = _int_or_none(error.get("code"))
            log.error("Graph API refused the message (HTTP %s, error %s): %s", resp.status_code, code,
                      error.get("message") or resp.text[:200])
            return SendResult(ok=False, error_code=code, error=error.get("message"))
        ids = [m.get("id") for m in body.get("messages") or [] if m.get("id")]
        return SendResult(ok=True, id=ids[0] if ids else None)

    def send_text(self, to: str, body: str) -> SendResult:
        return self._send({"recipient_type": "individual", "to": _wa_id(to), "type": "text",
                           "text": {"preview_url": False, "body": body}})

    def template_configured(self) -> bool:
        return bool(self._config.meta_template_name)

    @property
    def template_ref(self) -> str:
        return self._config.meta_template_name

    def send_template(self, to: str, variables: Mapping[str, str], language: str) -> SendResult:
        params = [{"type": "text", "text": str(variables[k])} for k in sorted(variables, key=_numeric_key)]
        template: dict[str, Any] = {"name": self._config.meta_template_name,
                                    "language": {"code": self._config.meta_template_language or language}}
        if params:
            template["components"] = [{"type": "body", "parameters": params}]
        return self._send({"recipient_type": "individual", "to": _wa_id(to), "type": "template",
                           "template": template})

    def render_webhook_reply(self, replies: list[str]) -> str:
        """Meta webhooks cannot carry replies (they are Graph API calls): the answer is just a 200."""
        return ""

    def use_http(self, client: Any) -> None:
        """Test seam: Graph API calls and media downloads go through ``client``."""
        self._http = client

    def mark_read(self, message_id: str) -> None:
        """Blue ticks: tells the candidate we read their message."""
        if self._config.meta_mark_read and message_id:
            self._send({"status": "read", "message_id": message_id})

    def fetch_media(self, media: MediaRef, max_bytes: int) -> tuple[bytes, str]:
        if not media.id:
            raise MediaError("attachment without media id")
        try:
            meta = self.http.get(self._url(media.id), headers=self._headers())
            if meta.status_code >= 400:
                raise MediaError(f"media lookup answered {meta.status_code}")
            info = meta.json()
            if int(info.get("file_size") or 0) > max_bytes:
                raise MediaError(f"attachment larger than {max_bytes} bytes")
            resp = self.http.get(info["url"], headers=self._headers())
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise MediaError(f"media download failed: {exc}") from exc
        if resp.status_code >= 400:
            raise MediaError(f"media download answered {resp.status_code}")
        if len(resp.content) > max_bytes:
            raise MediaError(f"attachment larger than {max_bytes} bytes")
        return resp.content, str(info.get("mime_type") or media.mime_type)


def _wa_id(number: str) -> str:
    """Graph API recipients are digits only (``34600111222``)."""
    return "".join(ch for ch in number if ch.isdigit())


def _e164(wa_id: Any) -> Optional[str]:
    digits = "".join(ch for ch in str(wa_id or "") if ch.isdigit())
    return f"+{digits}" if digits else None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numeric_key(key: str) -> tuple[int, str]:
    return (int(key), key) if str(key).isdigit() else (10**9, str(key))

