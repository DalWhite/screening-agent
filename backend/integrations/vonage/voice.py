"""Vonage Voice API: the call is a loop of NCCOs (JSON call-control objects).

* Answer URL (``GET`` with query parameters, or ``POST`` JSON): ``from``, ``to``,
  ``uuid``. We answer with an NCCO: ``talk`` (text-to-speech) then ``input``
  (speech recognition) whose ``eventUrl`` receives the result.
* Input event (``POST`` JSON): ``speech.results[].text`` / ``confidence``;
  silence is reported as ``speech.timeout_reason = "start_timeout"`` with no results.
* Event URL (``POST`` JSON): call status; ``completed`` (and failures) end the call.
* An NCCO without ``input`` ends the call after its ``talk``.
* Signed webhooks: ``Authorization: Bearer <JWT>`` (HS256, the application's
  signature secret) with ``payload_hash`` = SHA-256 of the body.

The screening logic is the provider-neutral `VoiceChannel`, exactly as with Twilio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ...channels.voice import VoiceAction, VoiceChannel

if TYPE_CHECKING:
    from ...channels.hub import ChannelHub

log = logging.getLogger("sazon.vonage")

ANSWER = "/api/channels/voice/vonage/answer"
INPUT = "/api/channels/voice/vonage/input"
EVENT = "/api/channels/voice/vonage/event"
FINAL_STATUSES = {"completed", "failed", "rejected", "busy", "cancelled", "timeout", "unanswered"}
JWT_MAX_AGE = 300


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def verify_signed_webhook(authorization: str, secret: str, body: bytes, now: Optional[float] = None) -> bool:
    """Vonage signed webhook: HS256 JWT made with the signature secret; ``payload_hash`` binds the body."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64url(header_b64))
        payload = json.loads(_b64url(payload_b64))
        signature = _b64url(signature_b64)
    except (ValueError, TypeError):
        return False
    if header.get("alg") != "HS256" or not secret:
        return False
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        return False
    issued = payload.get("iat")
    if isinstance(issued, (int, float)) and abs((now or time.time()) - issued) > JWT_MAX_AGE:
        return False
    if body:
        return hmac.compare_digest(str(payload.get("payload_hash", "")), hashlib.sha256(body).hexdigest())
    return True


def e164(number: Any) -> str:
    """Vonage numbers have no "+": ``34600111222`` -> ``+34600111222``."""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


class VonageVoice:
    name = "vonage"
    kind = "voice"

    def __init__(self, hub: "ChannelHub", channel: Optional[VoiceChannel] = None):
        self.config = hub.config
        self.channel = channel or VoiceChannel(hub.agent, hub.store, profile_id=hub.config.voice_profile or None)

    @property
    def configured(self) -> bool:
        return bool(self.config.vonage_signature_secret) or not self.config.vonage_validate_signatures

    def use_http(self, client: Any) -> None:
        """No outbound HTTP: Vonage drives the call through webhooks."""

    # ----- security -----

    async def _verified_body(self, request: Request) -> bytes:
        body = await request.body()
        if self.config.vonage_validate_signatures and not verify_signed_webhook(
                request.headers.get("authorization", ""), self.config.vonage_signature_secret, body):
            log.warning("rejected Vonage webhook with an invalid signature: %s", request.url.path)
            raise HTTPException(status_code=401, detail="invalid Vonage signature")
        return body

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        return data if isinstance(data, dict) else {}

    # ----- NCCO -----

    def render(self, action: VoiceAction, base_url: str) -> list[dict[str, Any]]:
        talk: dict[str, Any] = {"action": "talk", "text": action.speech, "language": action.locale,
                                "bargeIn": False}
        if self.config.voice_tts_voice.isdigit():
            talk["style"] = int(self.config.voice_tts_voice)
        ncco = [talk]
        if action.listen and action.session_id:
            event_url = f"{base_url}{INPUT}?session_id={quote(action.session_id)}&retries={action.retries}"
            ncco.append({"action": "input", "type": ["speech"], "eventUrl": [event_url], "eventMethod": "POST",
                         "speech": {"language": action.locale, "endOnSilence": 1.5, "startTimeout": 10,
                                    "maxDuration": 60}})
        return ncco  # without "input" the call ends after the talk

    def _base_url(self, request: Request) -> str:
        return self.config.public_base_url or str(request.base_url).rstrip("/")

    # ----- webhooks -----

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.api_route(ANSWER, methods=["GET", "POST"])
        async def answer(request: Request) -> JSONResponse:
            """A call comes in: the first NCCO."""
            body = await self._verified_body(request)
            data = dict(request.query_params) if request.method == "GET" else self._json(body)
            action = self.channel.start_call(e164(data.get("from")), str(data.get("uuid") or ""))
            return JSONResponse(self.render(action, self._base_url(request)))

        @router.post(INPUT)
        async def speech_input(request: Request, session_id: str, retries: int = 0) -> JSONResponse:
            """Result of an ``input`` action: the caller's words, or a timeout (silence)."""
            data = self._json(await self._verified_body(request))
            results = (data.get("speech") or {}).get("results") or []
            best = max(results, key=lambda r: float(r.get("confidence") or 0), default={})
            action = self.channel.speech(session_id, str(best.get("text") or ""), retries=retries,
                                         confidence=best.get("confidence"), call_id=str(data.get("uuid") or ""))
            return JSONResponse(self.render(action, self._base_url(request)))

        @router.post(EVENT)
        async def event(request: Request) -> Response:
            """Call status events; the end of the call is recorded."""
            data = self._json(await self._verified_body(request))
            if data.get("status") in FINAL_STATUSES:
                self.channel.call_ended(str(data.get("uuid") or ""), str(data["status"]))
            return Response(status_code=204)

        return router
