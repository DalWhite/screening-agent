from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from twilio.twiml.messaging_response import MessagingResponse

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.guardrails import redact_pii, screen_input
from ..screening.schema import Stage
from ..session_store import SessionStore
from ..storage import db
from .base import BaseChannel, normalize_contact, split_message
from .twilio_provider import TwilioProvider

log = logging.getLogger("sazon.whatsapp")


class WhatsAppChannel(BaseChannel):
    """Twilio WhatsApp adapter.

    Inbound: Twilio POSTs each candidate message to our webhook; we reply
    synchronously with TwiML when the agent answers within the webhook budget
    (Twilio times out at 15s). If the LLM is slower than that, the webhook
    returns an empty TwiML right away and the reply is pushed through the
    Twilio REST API as soon as it is ready.
    Outbound: re-engagement nudges also go through the REST API, because there
    is no open webhook to answer when the candidate is silent.
    """

    source = "whatsapp"

    def __init__(self, agent: ScreeningAgent, store: SessionStore, provider: TwilioProvider,
                 profile_id: Optional[str] = None):
        super().__init__(agent, store, profile_id)
        self.provider = provider
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.late_deliveries: set[asyncio.Future] = set()  # in-flight async replies (observable in tests)

    # ---------- inbound ----------

    def handle_inbound(self, from_addr: str, body: str) -> str:
        """Process one inbound WhatsApp message and return TwiML XML (synchronous)."""
        return self.twiml(self.process_inbound(from_addr, body))

    async def handle_inbound_async(self, from_addr: str, body: str,
                                   sync_timeout: Optional[float] = None) -> str:
        """Webhook entry point: TwiML reply within `sync_timeout`, REST delivery otherwise."""
        timeout = settings.whatsapp_sync_timeout if sync_timeout is None else sync_timeout
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self.process_inbound, from_addr, body)
        done, _ = await asyncio.wait({future}, timeout=timeout)
        if done:
            return self.twiml(future.result())

        contact = normalize_contact(from_addr)
        log.warning("whatsapp turn for %s exceeded %.1fs; reply will be pushed via REST", contact, timeout)
        self.late_deliveries.add(future)

        def _deliver(f: asyncio.Future) -> None:
            self.late_deliveries.discard(f)
            if f.cancelled() or f.exception() is not None:
                log.error("whatsapp turn for %s failed: %s", contact, f.exception() if not f.cancelled() else "cancelled")
                return
            loop.run_in_executor(None, self._push_replies, contact, f.result())

        future.add_done_callback(_deliver)
        return self.twiml([])  # empty <Response/> keeps Twilio happy

    def process_inbound(self, from_addr: str, body: str) -> list[str]:
        """Run the turn; returns the texts to send back (greeting(s) and/or reply)."""
        contact = normalize_contact(from_addr)
        body = (body or "").strip()
        with self._locks[contact]:  # one turn at a time per candidate
            resolved = self.resolve_session(contact)
            state = resolved.state

            replies = list(resolved.greetings)
            if resolved.created:
                # The first message is just the conversation trigger ("hola"):
                # store it for the transcript, answer with the agent's opening.
                self._record_trigger(state, body)
            elif body:
                reply, _ = self.run_turn(state, body)
                if reply:  # empty while a recruiter has taken the conversation over
                    replies.append(reply)

        log.info(
            "whatsapp turn session=%s created=%s resumed=%s stage=%s replies=%d chars=%d",
            state.id, resolved.created, resolved.resumed, state.stage,
            len(replies), sum(len(r) for r in replies),
        )
        return replies

    def _record_trigger(self, state: SessionState, body: str) -> None:
        if not body:
            return
        clean = redact_pii(screen_input(body).sanitized)
        state.history.append({"role": "user", "content": clean})
        state.last_candidate_ts = datetime.now(timezone.utc)
        db.append_message(state.id, "user", clean, Stage.INTRO)
        db.save_session(state)

    def twiml(self, replies: list[str]) -> str:
        response = MessagingResponse()
        for reply in replies:
            for chunk in split_message(reply, settings.whatsapp_char_limit):
                response.message(chunk)
        return str(response)

    # ---------- outbound push ----------

    def _push_replies(self, contact: str, replies: list[str]) -> None:
        for reply in replies:
            for chunk in split_message(reply, settings.whatsapp_char_limit):
                if not self.provider.send_whatsapp(contact, chunk):
                    if self.provider.template_required:
                        log.error("late whatsapp reply to %s refused: this sender only accepts templates, "
                                  "so interactive replies cannot be delivered — use the Sandbox or a "
                                  "Meta-verified sender", contact)
                    else:
                        log.error("late whatsapp reply to %s could not be sent — check TWILIO_WHATSAPP_FROM / "
                                  "credentials", contact)
                    return

    def send_outbound(self, state: SessionState, text: str) -> bool:
        """Push a message (e.g. a nudge) to the candidate. True if handed to Twilio.

        Business-initiated WhatsApp messages must be approved templates when the
        24h customer-service window is closed (or the sender is template-only).
        We try free-form first and fall back to TWILIO_WHATSAPP_CONTENT_SID.
        """
        if state.source != self.source or not state.contact:
            return False
        sid = self.provider.send_whatsapp(state.contact, text)
        if sid:
            log.info("whatsapp outbound sent session=%s sid=%s", state.id, sid)
            return True
        if self.provider.template_required:
            content_sid = settings.twilio_whatsapp_content_sid
            if not content_sid:
                log.error("session %s: WhatsApp requires a template for this push; set TWILIO_WHATSAPP_CONTENT_SID",
                          state.id)
                return False
            sid = self.provider.send_whatsapp(state.contact, content_sid=content_sid,
                                              content_variables=self._template_variables(state))
            if sid:
                log.info("whatsapp outbound sent as template session=%s sid=%s", state.id, sid)
                db.append_events(state.id, [{"type": "whatsapp_template_fallback",
                                             "payload": {"content_sid": content_sid}}], state.stage)
                return True
        return False

    def _template_variables(self, state: SessionState) -> dict[str, str]:
        """{{1}} = candidate first name (or a neutral fallback), {{2}} = agent name."""
        profile = self.agent.profile(state)
        name_field = profile.field_of_type("name") or "full_name"
        name = str(state.data.get(name_field) or "").strip().split(" ")[0]
        return {"1": name or ("there" if state.language.value == "en" else "hola"), "2": profile.persona.agent_name}
