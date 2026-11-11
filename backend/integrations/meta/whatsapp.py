"""WhatsApp through Meta's Cloud API: the webhook (handshake, signature, payloads) in front of
`MetaClient` (Graph API) and the provider-neutral `MessagingChannel`."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ...channels.messaging import MessagingChannel
from .client import MetaClient

if TYPE_CHECKING:
    from ...channels.hub import ChannelHub

log = logging.getLogger("sazon.meta")

WEBHOOK = "/api/channels/whatsapp/meta"


class MetaWhatsApp:
    name = "meta"
    kind = "whatsapp"

    def __init__(self, hub: "ChannelHub"):
        self.config = hub.config
        self.client = MetaClient(config=hub.config)
        self.channel = MessagingChannel(hub.agent, hub.store, self.client,
                                        profile_id=hub.config.whatsapp_profile or None, transcriber=hub.transcriber)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def use_http(self, client: Any) -> None:
        self.client.use_http(client)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get(WEBHOOK)
        def meta_verify(request: Request) -> Response:
            """Subscription handshake: echo hub.challenge when hub.verify_token matches."""
            q = request.query_params
            challenge = self.client.verify_subscription(q.get("hub.mode"), q.get("hub.verify_token"),
                                                        q.get("hub.challenge"))
            if challenge is None:
                raise HTTPException(status_code=403, detail="verify token mismatch")
            return Response(content=challenge, media_type="text/plain")

        @router.post(WEBHOOK)
        async def meta_webhook(request: Request) -> dict:
            """Candidate messages and delivery statuses (JSON, X-Hub-Signature-256)."""
            raw = await request.body()
            if not self.client.validate_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
                log.warning("rejected Meta webhook with invalid X-Hub-Signature-256")
                raise HTTPException(status_code=403, detail="invalid signature")
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid JSON") from exc
            messages, statuses = self.client.parse_webhook(payload)
            loop = asyncio.get_running_loop()
            for update in statuses:
                await loop.run_in_executor(None, self.channel.on_status, update)
            for message in messages:
                await self.channel.receive(message)  # replies go out through the Graph API
            return {"ok": True}

        return router
