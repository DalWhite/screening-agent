"""WhatsApp through Twilio: form webhooks in, replies as TwiML, pushes through the REST API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.background import BackgroundTask

from ...channels.contracts import InboundMessage, MediaRef, StatusUpdate
from ...channels.messaging import MessagingChannel
from .webhooks import WHATSAPP_INBOUND, WHATSAPP_STATUS, root_router, shared_client, twilio_form, xml

if TYPE_CHECKING:
    from ...channels.hub import ChannelHub


class TwilioWhatsApp:
    """The Twilio WhatsApp adapter: parses Twilio's webhooks for the `MessagingChannel`."""

    name = "twilio"
    kind = "whatsapp"

    def __init__(self, hub: "ChannelHub"):
        self.config = hub.config
        self.client = shared_client(hub)
        self.channel = MessagingChannel(hub.agent, hub.store, self.client,
                                        profile_id=hub.config.whatsapp_profile or None, transcriber=hub.transcriber)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def use_http(self, client: Any) -> None:
        self.client.use_http(client)

    @staticmethod
    def parse(form: dict[str, str]) -> InboundMessage:
        """Twilio WhatsApp webhook fields -> provider-neutral message (text, voice notes, media)."""
        try:
            count = int(form.get("NumMedia") or 0)
        except ValueError:
            count = 0
        # Twilio does not tell voice notes from audio files: on WhatsApp, audio is nearly always a voice note
        media = [MediaRef(url=form.get(f"MediaUrl{i}"), mime_type=form.get(f"MediaContentType{i}", ""),
                          voice=form.get(f"MediaContentType{i}", "").startswith("audio/"))
                 for i in range(min(count, 10))]
        return InboundMessage(contact=form.get("From", ""), text=form.get("Body", ""), media=media,
                              message_id=form.get("MessageSid") or None, profile_name=form.get("ProfileName"),
                              provider="twilio")

    async def answer(self, form: dict[str, str]) -> Response:
        message = self.parse(form)
        replies = await self.channel.receive(message, hold=True)
        # the turn counts as answered once Twilio has the TwiML: only then may pushes for this candidate go out
        return xml(self.client.render_webhook_reply(replies),
                    BackgroundTask(self.channel.release, message.contact))

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(WHATSAPP_INBOUND)
        async def whatsapp_inbound(request: Request) -> Response:
            """Twilio WhatsApp webhook: one inbound candidate message per request."""
            return await self.answer(await twilio_form(request, self.client, self.config.public_base_url))

        @router.post(WHATSAPP_STATUS)
        async def whatsapp_status(request: Request) -> Response:
            """Status callback for messages we pushed (delivered, read, failed + ErrorCode)."""
            form = await twilio_form(request, self.client, self.config.public_base_url)
            try:
                code = int(form["ErrorCode"]) if form.get("ErrorCode") else None
            except ValueError:
                code = None
            update = StatusUpdate(provider="twilio", external_id=form.get("MessageSid", ""),
                                  status=form.get("MessageStatus", ""), error_code=code)
            await asyncio.get_running_loop().run_in_executor(None, self.channel.on_status, update)
            return Response(status_code=204)

        return router

    def extra_routers(self) -> dict[str, Any]:
        return {"twilio-root": root_router}
