from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from twilio.base.exceptions import TwilioRestException
from twilio.http import HttpClient
from twilio.http.http_client import TwilioHttpClient
from twilio.http.response import Response as TwilioResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from ...channels.contracts import MediaError, MediaRef, SendResult
from ...config import settings

log = logging.getLogger("sazon.twilio")

# Twilio error codes meaning "WhatsApp wants an approved Content Template here".
# 21654: ContentSid required for this sender; 63016: free-form message outside the 24h window.
TEMPLATE_REQUIRED_CODES = {21654, 63016}


class BaseUrlHttpClient(TwilioHttpClient):
    """Twilio SDK HTTP layer that sends ``https://*.twilio.com`` requests to another base URL.

    Used with ``TWILIO_API_BASE_URL`` to point the real SDK at the local channel
    simulator (``python -m simulators``), so outbound code runs unmodified.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        super().__init__(timeout=timeout)
        self.base_url = base_url.rstrip("/")

    def request(self, method, url, *args, **kwargs):
        parts = urlsplit(url)
        if (parts.hostname or "").endswith("twilio.com"):
            url = f"{self.base_url}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        return super().request(method, url, *args, **kwargs)


class HttpxHttpClient(HttpClient):
    """Twilio SDK HTTP layer on top of any httpx-compatible client (``TwilioClient.use_http``).

    Requests to ``*.twilio.com`` go to ``base_url`` when given (a simulator)."""

    def __init__(self, client: Any, base_url: str = ""):
        super().__init__(logging.getLogger("sazon.twilio.http"), False)
        self.http = client
        self.base_url = base_url.rstrip("/")

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None,
                allow_redirects=False) -> TwilioResponse:
        parts = urlsplit(url)
        if self.base_url and (parts.hostname or "").endswith("twilio.com"):
            url = f"{self.base_url}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        resp = self.http.request(method, url, params=params, data=data, headers=headers, auth=auth)
        return TwilioResponse(resp.status_code, resp.text, resp.headers)


class TwilioClient:
    """Our client for Twilio's API (a thin wrapper around the Twilio SDK).

    Everything the rest of the codebase needs from Twilio goes through this
    class: outbound WhatsApp messages and webhook signature validation. Tests
    inject a fake client / settings object, so no test ever talks to Twilio.
    """

    name = "twilio"
    kind = "whatsapp"  # the channel it carries
    replies_in_webhook = True  # replies ride the webhook answer (TwiML)
    window_error_codes = frozenset(TEMPLATE_REQUIRED_CODES)
    template_setting = "TWILIO_WHATSAPP_CONTENT_SID"
    STATUS_PATH = "/api/channels/whatsapp/status"

    def __init__(self, config: Any = settings, client: Optional[Client] = None, media_http: Optional[Any] = None):
        self._config = config
        self._client = client
        self._media_http = media_http  # httpx.Client-compatible (tests: the simulator)

    # ---------- configuration ----------

    @property
    def uses_api_key(self) -> bool:
        return bool(getattr(self._config, "twilio_api_key_sid", "") and getattr(self._config, "twilio_api_key_secret", ""))

    @property
    def configured(self) -> bool:
        if not self._config.twilio_account_sid:
            return False
        return bool(self._config.twilio_auth_token) or self.uses_api_key

    @property
    def client(self) -> Client:
        if self._client is None:
            if not self.configured:
                raise RuntimeError("Twilio credentials missing (TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN or API key)")
            extra: dict[str, Any] = {}
            base_url = getattr(self._config, "twilio_api_base_url", "")
            if base_url:  # local Twilio simulator: same SDK, another host
                log.warning("Twilio REST calls go to %s (simulator), not to Twilio", base_url)
                extra["http_client"] = BaseUrlHttpClient(base_url)
            if self.uses_api_key:
                # API key auth: username=SK..., password=secret, account_sid=AC...
                self._client = Client(self._config.twilio_api_key_sid, self._config.twilio_api_key_secret,
                                      self._config.twilio_account_sid, **extra)
            else:
                self._client = Client(self._config.twilio_account_sid, self._config.twilio_auth_token, **extra)
        return self._client

    def check_credentials(self) -> dict:
        """Fetch the account record — the cheapest call that proves the credentials work."""
        account = self.client.api.v2010.accounts(self._config.twilio_account_sid).fetch()
        return {
            "sid": account.sid,
            "friendly_name": account.friendly_name,
            "status": account.status,
            "type": account.type,  # "Trial" or "Full"
        }

    # ---------- outbound ----------

    def send_whatsapp(self, to: str, body: str = "", content_sid: Optional[str] = None,
                      content_variables: Optional[Mapping[str, str]] = None,
                      status_callback: Optional[str] = None) -> Optional[str]:
        """Send an outbound WhatsApp message; returns the message SID or None.

        Free-form `body` by default; pass `content_sid` (+ `content_variables`)
        to send an approved Content Template instead. `to` may be a bare E.164
        number or already carry the `whatsapp:` prefix. Failures are logged and
        swallowed — channel delivery must never crash the agent. The Twilio
        error code of the last failure is kept in `last_error_code` so callers
        can decide on a fallback (e.g. retry with a template).
        """
        self.last_error_code: Optional[int] = None
        if not self.configured or not self._config.twilio_whatsapp_from:
            log.warning("whatsapp send skipped: Twilio not configured")
            return None
        to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        from_addr = self._config.twilio_whatsapp_from
        if not from_addr.startswith("whatsapp:"):
            from_addr = f"whatsapp:{from_addr}"
        params: dict[str, Any] = {"to": to_addr, "from_": from_addr}
        if content_sid:
            params["content_sid"] = content_sid
            params["content_variables"] = json.dumps(dict(content_variables or {}), ensure_ascii=False)
        else:
            params["body"] = body
        if status_callback:
            params["status_callback"] = status_callback
        try:
            message = self.client.messages.create(**params)
            return getattr(message, "sid", None)
        except TwilioRestException as exc:
            self.last_error_code = exc.code
            if exc.code in TEMPLATE_REQUIRED_CODES:
                log.warning("whatsapp free-form message to %s refused (error %s): a Content Template is required",
                            to_addr, exc.code)
            else:
                log.error("failed to send whatsapp message to %s: %s (error %s)", to_addr, exc.msg, exc.code)
            return None
        except Exception:
            log.exception("failed to send whatsapp message to %s", to_addr)
            return None

    @property
    def template_required(self) -> bool:
        return getattr(self, "last_error_code", None) in TEMPLATE_REQUIRED_CODES

    # ---------- WhatsAppProvider interface (see messaging.py) ----------

    @property
    def status_callback_url(self) -> Optional[str]:
        """Delivery receipts need a public URL; without PUBLIC_BASE_URL Twilio cannot call us back."""
        base = getattr(self._config, "public_base_url", "")
        return f"{base}{self.STATUS_PATH}" if base else None

    def send_text(self, to: str, body: str) -> SendResult:
        sid = self.send_whatsapp(to, body, status_callback=self.status_callback_url)
        return SendResult(ok=bool(sid), id=sid, error_code=getattr(self, "last_error_code", None))

    def template_configured(self) -> bool:
        return bool(self._config.twilio_whatsapp_content_sid)

    @property
    def template_ref(self) -> str:
        return self._config.twilio_whatsapp_content_sid

    def send_template(self, to: str, variables: Mapping[str, str], language: str) -> SendResult:
        sid = self.send_whatsapp(to, content_sid=self._config.twilio_whatsapp_content_sid,
                                 content_variables=variables, status_callback=self.status_callback_url)
        return SendResult(ok=bool(sid), id=sid, error_code=getattr(self, "last_error_code", None))

    def fetch_media(self, media: MediaRef, max_bytes: int) -> tuple[bytes, str]:
        """Download a MediaUrl (HTTP Basic with the account credentials; Twilio redirects to storage)."""
        if not media.url:
            raise MediaError("attachment without URL")
        url = media.url
        base_url = getattr(self._config, "twilio_api_base_url", "")
        parts = urlsplit(url)
        if base_url and (parts.hostname or "").endswith("twilio.com"):
            url = f"{base_url}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        if self.uses_api_key:
            auth = (self._config.twilio_api_key_sid, self._config.twilio_api_key_secret)
        else:
            auth = (self._config.twilio_account_sid, self._config.twilio_auth_token)
        http = self._media_http or httpx.Client(timeout=15, follow_redirects=True)
        try:
            resp = http.get(url, auth=auth)
        except httpx.HTTPError as exc:
            raise MediaError(f"media download failed: {exc}") from exc
        if resp.status_code >= 400:
            raise MediaError(f"media download answered {resp.status_code}")
        if len(resp.content) > max_bytes:
            raise MediaError(f"attachment larger than {max_bytes} bytes")
        return resp.content, resp.headers.get("content-type", media.mime_type).split(";")[0] or media.mime_type

    def mark_read(self, message_id: str) -> None:
        """Twilio has no read receipts for inbound WhatsApp messages."""

    def render_webhook_reply(self, replies: list[str]) -> str:
        """The replies as TwiML ``<Message>``s (long ones split: WhatsApp caps bodies at 1600 chars)."""
        from twilio.twiml.messaging_response import MessagingResponse

        from ...channels.base import split_message

        response = MessagingResponse()
        for reply in replies:
            for chunk in split_message(reply, getattr(self._config, "whatsapp_char_limit", 1500)):
                response.message(chunk)
        return str(response)

    def use_http(self, client: Any) -> None:
        """Test seam: the SDK's REST calls and media downloads go through ``client``."""
        http = HttpxHttpClient(client, getattr(self._config, "twilio_api_base_url", ""))
        if self.uses_api_key:
            self._client = Client(self._config.twilio_api_key_sid, self._config.twilio_api_key_secret,
                                  self._config.twilio_account_sid, http_client=http)
        else:
            self._client = Client(self._config.twilio_account_sid, self._config.twilio_auth_token, http_client=http)
        self._media_http = client

    # ---------- webhook security ----------

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        """Validate Twilio's X-Twilio-Signature header for an incoming webhook."""
        if not self._config.twilio_validate_signatures:
            return True
        if not self._config.twilio_auth_token:
            log.warning("signature validation requested but TWILIO_AUTH_TOKEN is empty; rejecting")
            return False
        validator = RequestValidator(self._config.twilio_auth_token)
        return bool(validator.validate(url, dict(params), signature))
