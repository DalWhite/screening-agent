from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from ..config import settings

log = logging.getLogger("sazon.twilio")

# Twilio error codes meaning "WhatsApp wants an approved Content Template here".
# 21654: ContentSid required for this sender; 63016: free-form message outside the 24h window.
TEMPLATE_REQUIRED_CODES = {21654, 63016}


class TwilioProvider:
    """Thin wrapper around the Twilio SDK.

    Everything the rest of the codebase needs from Twilio goes through this
    class: outbound WhatsApp messages and webhook signature validation. Tests
    inject a fake client / settings object, so no test ever talks to Twilio.
    """

    def __init__(self, config: Any = settings, client: Optional[Client] = None):
        self._config = config
        self._client = client

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
            if self.uses_api_key:
                # API key auth: username=SK..., password=secret, account_sid=AC...
                self._client = Client(self._config.twilio_api_key_sid, self._config.twilio_api_key_secret,
                                      self._config.twilio_account_sid)
            else:
                self._client = Client(self._config.twilio_account_sid, self._config.twilio_auth_token)
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
                      content_variables: Optional[Mapping[str, str]] = None) -> Optional[str]:
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
