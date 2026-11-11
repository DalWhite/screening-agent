"""Twilio webhooks: signature validation, the shared API client, the bare-host safety net.

Twilio posts form-encoded webhooks signed with ``X-Twilio-Signature`` (HMAC of the
URL it called + the form, with the auth token). Both Twilio adapters
(`whatsapp.TwilioWhatsApp`, `voice.TwilioVoice`) use these helpers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.background import BackgroundTask

from .client import TwilioClient

if TYPE_CHECKING:
    from ...channels.hub import ChannelHub

log = logging.getLogger("sazon.twilio")

WHATSAPP_INBOUND = "/api/channels/whatsapp/inbound"
WHATSAPP_STATUS = "/api/channels/whatsapp/status"
VOICE_INBOUND = "/api/channels/voice/inbound"
VOICE_COLLECT = "/api/channels/voice/collect"
VOICE_STATUS = "/api/channels/voice/status"
FINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


def shared_client(hub: "ChannelHub") -> TwilioClient:
    """One Twilio API client per hub, shared by the WhatsApp and voice adapters."""
    return hub.shared("twilio", lambda: TwilioClient(config=hub.config))


async def twilio_form(request: Request, client: TwilioClient, public_base_url: str) -> dict[str, str]:
    """Read Twilio's form-encoded webhook body and enforce ``X-Twilio-Signature``.

    Twilio signs the URL it called: behind a proxy or tunnel that is ``PUBLIC_BASE_URL``.
    """
    form = {k: str(v) for k, v in (await request.form()).items()}
    if public_base_url:
        query = f"?{request.url.query}" if request.url.query else ""
        url = f"{public_base_url}{request.url.path}{query}"
    else:
        url = str(request.url)
    if not client.validate_signature(url, form, request.headers.get("X-Twilio-Signature", "")):
        log.warning("rejected webhook with invalid Twilio signature: %s", request.url.path)
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


def xml(body: str, background: Optional[BackgroundTask] = None) -> Response:
    return Response(content=body, media_type="application/xml", background=background)


# ---------- the bare-host safety net ----------

def root_router(hub: "ChannelHub") -> APIRouter:
    """``POST /``: a webhook configured as the bare host URL in the Twilio console.

    Twilio posts form-encoded bodies; they are recognised by shape and routed to
    the right Twilio adapter, with a warning pointing at the explicit URLs.
    """
    router = APIRouter()

    @router.post("/")
    async def twilio_root_dispatch(request: Request) -> Response:
        from .voice import TwilioVoice
        from .whatsapp import TwilioWhatsApp

        whatsapp = hub.adapter("whatsapp")
        voice = hub.adapter("voice")
        client = (whatsapp if isinstance(whatsapp, TwilioWhatsApp) else voice).client
        form = await twilio_form(request, client, hub.config.public_base_url)
        base = hub.config.public_base_url or "<PUBLIC_BASE_URL>"
        if "MessageSid" in form or "Body" in form:
            if not isinstance(whatsapp, TwilioWhatsApp):
                raise HTTPException(status_code=404, detail="WhatsApp does not run on Twilio (WHATSAPP_PROVIDER)")
            log.warning("Twilio messaging webhook hit '/': configure %s%s instead", base, WHATSAPP_INBOUND)
            return await whatsapp.answer(form)
        if "CallSid" in form and isinstance(voice, TwilioVoice):
            if form.get("CallStatus") in FINAL_CALL_STATUSES:
                voice.handle_call_ended(form["CallSid"], form["CallStatus"])
                return Response(status_code=204)
            log.warning("Twilio voice webhook hit '/': configure %s%s instead", base, VOICE_INBOUND)
            return xml(voice.handle_call_start(form.get("From", ""), form.get("CallSid", "")))
        raise HTTPException(
            status_code=404,
            detail=f"Not a Twilio webhook. Channel endpoints: {WHATSAPP_INBOUND}, {VOICE_INBOUND}, {VOICE_STATUS}",
        )

    return router
