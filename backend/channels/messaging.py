from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.guardrails import redact_pii, screen_input
from ..session_store import SessionStore
from ..identity import service as identity
from ..screening.profile import localize as localize_text
from ..storage import db
from .base import BaseChannel, ResolvedSession, normalize_contact, split_message
from .contracts import (InboundMessage, MediaError, MessagingClient, SendResult, StatusUpdate, Transcriber,
                        TranscriptionError)

log = logging.getLogger("sazon.whatsapp")

MEDIA_LABELS = {"image": "Foto", "video": "Vídeo", "document": "Documento", "sticker": "Sticker",
                "audio": "Audio", "location": "Ubicación", "contacts": "Contacto"}

MEDIA_NOTICE = {
    "es": "Solo puedo leer mensajes de texto y notas de voz, así que no he podido ver lo que has enviado (no lo guardamos).",
    "en": "I can only read text messages and voice notes, so I couldn't see what you sent (we don't keep it).",
}
VOICE_NOTE_FAILED = {
    "es": "No he podido entender tu nota de voz. ¿Me lo escribes o lo intentas de nuevo?",
    "en": "I couldn't make out your voice note. Could you type it or try again?",
}

# Messages that only say "hello / I'm back / the call dropped": after a channel switch they get the
# pending question again instead of being read as an answer. Words that can be answers ("sí", "no",
# "mañana", "tarde", numbers...) are deliberately absent: anything else makes the message a turn.
_GREETING_WORDS = {
    # greetings
    "hola", "holi", "holaa", "buenas", "buenos", "buen", "dias", "tardes", "noches", "hey", "ey", "hi", "hello",
    "hallo", "saludos", "que", "tal", "good", "morning", "afternoon", "evening", "there",
    # back / continuing
    "ya", "estoy", "aqui", "sigo", "sigamos", "seguimos", "seguir", "continuar", "continuamos", "de", "nuevo", "por",
    "otra", "vez", "mejor", "escribo", "te", "os", "whatsapp", "im", "i", "am", "back", "again", "here", "lets",
    "continue", "me",
    # the call dropped
    "se", "ha", "cortado", "corto", "cortó", "la", "llamada", "colgado", "colgo", "colgaste", "cobertura", "sin",
    "bateria", "quede", "the", "call", "dropped", "cut", "off", "got", "was", "disconnected", "lost", "connection",
    # politeness
    "perdon", "perdona", "disculpa", "disculpad", "sorry", "ok", "vale", "gracias", "thanks",
}


def is_greeting_only(text: str) -> bool:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z]+", plain)
    return bool(words) and len(words) <= 12 and all(w in _GREETING_WORDS for w in words)


@dataclass
class TurnResult:
    session_id: Optional[str]
    replies: list[str] = field(default_factory=list)


@dataclass
class _Read:
    """What a WhatsApp message means for the agent."""

    text: str  # typed text, a media caption, or a voice note transcript
    kind: str = "text"  # text | voice_note | media
    meta: dict[str, Any] = field(default_factory=dict)
    placeholder: str = ""  # stored in the transcript when there is no text
    failed: bool = False  # a voice note we could not transcribe
    notice: Optional[dict[str, str]] = None  # localized notice the candidate must get


class MessagingChannel(BaseChannel):
    """A messaging channel (WhatsApp today), independent of the provider that carries it.

    Integrations (``backend/integrations/<vendor>/``) turn their webhooks into
    `InboundMessage` / `StatusUpdate` and implement `MessagingClient` for
    sending. The channel never sees a provider's wire format: it asks the
    provider to render the webhook answer (``render_webhook_reply``) and reads
    its capabilities (``replies_in_webhook``, ``window_error_codes``, templates).

    Inbound: the turn runs in a worker thread within the webhook budget (Twilio
    gives up at 15s). Providers that can carry replies in the webhook answer
    (Twilio: TwiML) get them there; the others (Meta) get them sent through
    their API. Turns slower than the budget are answered through the API.

    Outbound (nudges, recruiter messages, check decisions): free-form inside
    the 24h customer-service window, the approved template outside it. Every
    push is recorded in `channel_deliveries`; provider status callbacks update
    it and, when a push fails after being accepted (Meta 131047, Twilio 63016),
    the message goes back to the queue and the template is sent instead.

    Voice notes are downloaded, transcribed and answered like text (the audio
    is never stored); other media get a placeholder and a polite notice.
    """

    source = "whatsapp"
    WINDOW = timedelta(hours=24)  # free-form messages only within this window after the candidate's last one

    def __init__(self, agent: ScreeningAgent, store: SessionStore, provider: MessagingClient,
                 profile_id: Optional[str] = None, transcriber: Optional[Transcriber] = None):
        super().__init__(agent, store, profile_id)
        self.provider = provider
        self.source = getattr(provider, "kind", type(self).source)
        self.transcriber = transcriber or Transcriber()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.late_deliveries: set[asyncio.Future] = set()  # in-flight async replies (observable in tests)
        self._seen: OrderedDict[str, None] = OrderedDict()  # provider message ids (webhook retries)
        self._seen_lock = threading.Lock()
        # Receipts that arrived before we knew the message id (the send call had not returned yet):
        # replayed as soon as the delivery is recorded. Providers do not guarantee that order.
        self._early_receipts: OrderedDict[tuple[str, str], list[StatusUpdate]] = OrderedDict()
        # Turns whose replies are still on their way, per contact. Business-initiated messages wait
        # for them, so e.g. a check decision never overtakes the reply to the candidate's last answer.
        self._in_flight: dict[str, int] = defaultdict(int)
        self._idle = threading.Condition()

    # ---------- inbound ----------

    def handle_inbound(self, from_addr: str, body: str) -> str:
        """Process one inbound text and return the provider's webhook answer (synchronous)."""
        return self.provider.render_webhook_reply(self.process_inbound(from_addr, body))

    async def handle_inbound_async(self, from_addr: str, body: str,
                                   sync_timeout: Optional[float] = None) -> str:
        """Text webhook: the reply in the webhook answer within `sync_timeout`, through the API otherwise."""
        message = InboundMessage(contact=from_addr, text=body, provider=self.provider.name)
        return self.provider.render_webhook_reply(await self.receive(message, sync_timeout))

    async def receive(self, message: InboundMessage, sync_timeout: Optional[float] = None,
                      hold: bool = False) -> list[str]:
        """Webhook entry point for any provider.

        Returns the replies to put in the webhook answer: the turn's replies when
        it finished in time and the provider can carry them (Twilio), otherwise
        [] — the replies are then sent through the provider API.

        With ``hold=True`` the turn stays "in flight" after returning replies, until
        the caller calls :meth:`release` once the webhook answer has been sent.
        """
        timeout = settings.whatsapp_sync_timeout if sync_timeout is None else sync_timeout
        contact = normalize_contact(message.contact)
        loop = asyncio.get_running_loop()
        self._begin(contact)
        release = True
        try:
            future = loop.run_in_executor(None, self.process_message, message)
            done, _ = await asyncio.wait({future}, timeout=timeout)
            if done:
                result = future.result()
                if self.provider.replies_in_webhook:
                    release = not hold
                    return result.replies
                await loop.run_in_executor(None, self.deliver, message.contact, result.replies, result.session_id)
                return []

            log.warning("whatsapp turn for %s exceeded %.1fs; reply will be pushed via the provider API",
                        contact, timeout)
            self.late_deliveries.add(future)
            release = False

            def _deliver(f: asyncio.Future) -> None:
                self.late_deliveries.discard(f)
                if f.cancelled() or f.exception() is not None:
                    log.error("whatsapp turn for %s failed: %s", contact,
                              f.exception() if not f.cancelled() else "cancelled")
                    self.release(contact)
                    return
                result = f.result()

                def _push_and_release() -> None:
                    try:
                        self.deliver(contact, result.replies, result.session_id)
                    finally:
                        self.release(contact)

                loop.run_in_executor(None, _push_and_release)

            future.add_done_callback(_deliver)
            return []
        finally:
            if release:
                self.release(contact)

    def _begin(self, contact: str) -> None:
        with self._idle:
            self._in_flight[contact] += 1

    def release(self, contact: str) -> None:
        """The replies of one of this contact's turns are out (see ``receive(hold=True)``)."""
        contact = normalize_contact(contact)
        with self._idle:
            self._in_flight[contact] = max(self._in_flight[contact] - 1, 0)
            if not self._in_flight[contact]:
                del self._in_flight[contact]
            self._idle.notify_all()

    def wait_until_idle(self, contact: str, timeout: Optional[float] = None) -> bool:
        """Block until no turn of this contact is being answered (True) or ``timeout`` (False)."""
        contact = normalize_contact(contact)
        limit = settings.whatsapp_sync_timeout + 5 if timeout is None else timeout
        with self._idle:
            return self._idle.wait_for(lambda: not self._in_flight.get(contact), timeout=limit)

    def process_inbound(self, from_addr: str, body: str) -> list[str]:
        """Run a text turn; returns the texts to send back (greeting(s) and/or reply)."""
        return self.process_message(InboundMessage(contact=from_addr, text=body, provider=self.provider.name)).replies

    def process_message(self, message: InboundMessage) -> TurnResult:
        if not message.message_id:
            return self._process(message)
        # provider retries (same message id) are answered once, also across restarts
        if not self._first_time(message.message_id) or not db.claim_inbound(self.provider.name, message.message_id):
            log.info("whatsapp message %s already processed (provider retry): ignored", message.message_id)
            return TurnResult(session_id=None)
        try:
            result = self._process(message)
        except Exception:
            db.finish_inbound(self.provider.name, message.message_id, processed=False)  # the retry may succeed
            with self._seen_lock:
                self._seen.pop(message.message_id, None)
            raise
        db.finish_inbound(self.provider.name, message.message_id)
        return result

    def _process(self, message: InboundMessage) -> TurnResult:
        contact = normalize_contact(message.contact)
        with self._locks[contact]:  # one turn at a time per candidate
            resolved = self.resolve_session(contact, store_greetings=False)
            state = resolved.state
            self.set_last_inbound(state, datetime.now(timezone.utc))  # the candidate (re)opened the window
            # Messages we could not push earlier (provider down, window closed, the candidate was on
            # the phone...) go first: the candidate just opened the window.
            replies = self.pending_nudges(state)
            read = self._read(message, state)
            replies += self._respond(state, resolved, read)
        if message.message_id:
            try:
                self.provider.mark_read(message.message_id)
            except Exception:  # read receipts are cosmetic
                log.exception("could not mark %s as read", message.message_id)
        log.info(
            "whatsapp turn session=%s kind=%s created=%s resumed=%s switched=%s stage=%s replies=%d chars=%d",
            state.id, read.kind, resolved.created, resolved.resumed, resolved.switched, state.stage,
            len(replies), sum(len(r) for r in replies),
        )
        return TurnResult(session_id=state.id, replies=replies)

    def _respond(self, state: SessionState, resolved: ResolvedSession, read: _Read) -> list[str]:
        lang = state.language.value
        notice = "" if state.bot_paused else _localize(read.notice or (VOICE_NOTE_FAILED if read.failed else None), lang)
        greeted = bool(resolved.greetings)
        trigger = resolved.created or resolved.first_contact or (
            greeted and (read.failed or not read.text or is_greeting_only(read.text)))
        if trigger:
            # The message opens (or re-opens) the conversation: store it, answer with the greeting,
            # which carries the opening or the pending question.
            self._record_inbound(state, read, before=len(resolved.greetings))
            self.store_greetings(resolved)
            return [t for t in (notice, *resolved.greetings) if t]

        preface: tuple[str, ...] = ()
        if greeted:
            # Back from another channel (or after a pause) with an actual answer: process it, so
            # nothing the candidate says is lost; "welcome back" replaces the re-asked question.
            self.discard_greetings(resolved)
            preface = (self.agent.resume_text(state),)

        if read.text:
            notices = (notice,) if notice else ()
            stage, tries = state.stage, state.attempts_for(state.stage)
            reply, _ = self.run_turn(state, read.text, kind=read.kind, meta=read.meta, preface=preface + notices)
            if greeted and state.stage == stage and state.attempts_for(stage) > tries:
                # back from another channel / a pause with something that is not an answer
                # ("perdona, no te oía bien"): re-asked, but that does not count as a failed try
                state.attempts[stage] = tries
                db.save_session(state)
            return [t for t in (*preface, *notices, reply) if t]

        # nothing the agent can read: a photo without caption, a voice note we could not transcribe
        self._record_inbound(state, read)
        if state.bot_paused:
            return []  # the recruiter sees the placeholder in the transcript
        text = f"{notice} {self.agent.question_text(state)}".strip()
        self.record_assistant(state, text, kind="notice")
        return [*preface, text]

    def _read(self, message: InboundMessage, state: SessionState) -> _Read:
        audio = message.audio
        if audio is not None:
            return self._transcribe(message, audio)
        if message.media or message.kind:
            media_type = message.media[0].media_type if message.media else message.kind or "unsupported"
            meta = {"media_type": media_type, "stored": False, "provider": self.provider.name}
            if message.media:
                meta["mime"] = message.media[0].mime_type
            if message.text:
                meta["caption"] = True
            return _Read(text=message.text, kind="media", meta=meta,
                         placeholder=f"[{MEDIA_LABELS.get(media_type, 'Adjunto')}]", notice=MEDIA_NOTICE)
        return _Read(text=message.text)

    def _transcribe(self, message: InboundMessage, audio) -> _Read:
        meta: dict[str, Any] = {"media_type": "voice_note" if audio.voice else "audio",
                                "mime": audio.mime_type, "provider": self.provider.name, "audio_stored": False}
        if not self.transcriber.available:
            meta.update(transcribed=False, error="speech-to-text not configured")
            return _Read(text="", kind="voice_note", meta=meta, placeholder="[Nota de voz sin transcribir]", failed=True)
        try:
            data, mime = self.provider.fetch_media(audio, settings.whatsapp_media_max_bytes)
            result = self.transcriber.transcribe(data, mime)
        except (MediaError, TranscriptionError) as exc:
            log.warning("voice note from %s not transcribed: %s", normalize_contact(message.contact), exc)
            meta.update(transcribed=False, error=str(exc)[:200])
            return _Read(text="", kind="voice_note", meta=meta, placeholder="[Nota de voz sin transcribir]", failed=True)
        meta.update(transcribed=True, stt=self.transcriber.name, stt_model=self.transcriber.model,
                    bytes=len(data), language=result.language, duration=result.duration)
        text = f"{result.text} {message.text}".strip() if message.text else result.text
        return _Read(text=text, kind="voice_note", meta={k: v for k, v in meta.items() if v is not None},
                     placeholder="[Nota de voz]")

    def _record_inbound(self, state: SessionState, read: _Read, before: int = 0) -> None:
        """Store a candidate message that is not a turn (trigger, unreadable media).

        ``before``: greetings already in the agent's history that the candidate's
        message actually preceded.
        """
        content = redact_pii(screen_input(read.text).sanitized) if read.text else read.placeholder
        if not content:
            return
        state.history.insert(max(len(state.history) - before, 0), {"role": "user", "content": content})
        state.last_candidate_ts = datetime.now(timezone.utc)
        db.append_message(state.id, "user", content, state.stage, channel=self.source, kind=read.kind, meta=read.meta)
        db.save_session(state)

    def _first_time(self, message_id: str) -> bool:
        with self._seen_lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            if len(self._seen) > 5000:
                self._seen.popitem(last=False)
            return True

    # ---------- per-channel state (kept in the session, owned by the channel) ----------

    def last_inbound(self, state: SessionState) -> Optional[datetime]:
        """When the candidate last wrote on this channel (None = unknown, e.g. older sessions). The
        24h window is the person's, not the application's: another application's message counts."""
        value = state.channel_state.get(self.source, {}).get("inbound_at")
        person = identity.channel_info(state.candidate_id, self.source).get("inbound_at")
        if person and (not value or person > value):
            value = person
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    def set_last_inbound(self, state: SessionState, when: datetime) -> None:
        state.channel_state.setdefault(self.source, {})["inbound_at"] = when.isoformat()
        if state.candidate_id:
            info = identity.channel_info(state.candidate_id, self.source)
            # keep the later of the two: a known-closed window (an async failure) rewinds the session only
            if when.isoformat() >= info.get("inbound_at", "") or when < datetime.now(timezone.utc) - self.WINDOW:
                identity.note_channel(state.candidate_id, self.source, inbound_at=when.isoformat())

    def window_open(self, state: SessionState, now: Optional[datetime] = None,
                    margin: timedelta = timedelta(minutes=5)) -> Optional[bool]:
        """Free-form messages allowed? True/False when we know, None when we don't (the provider tells us)."""
        if not self.provider.window_error_codes:
            return True  # a channel without a customer-service window
        last = self.last_inbound(state)
        if last is None:
            return None
        return (now or datetime.now(timezone.utc)) - last < self.WINDOW - margin

    # ---------- outbound push ----------

    def deliver(self, contact: str, replies: list[str], session_id: Optional[str] = None) -> bool:
        """Send turn replies through the provider API (Meta, or Twilio after a slow turn).
        A failure puts the unsent replies back in the queue for the candidate's next message."""
        contact = normalize_contact(contact)
        for index, reply in enumerate(replies):
            for chunk in split_message(reply, settings.whatsapp_char_limit):
                result = self.provider.send_text(contact, chunk)
                self._record(session_id, contact, "reply", reply, result)
                if not result.ok:
                    if result.error_code in self.provider.window_error_codes:
                        log.error("whatsapp reply to %s refused (error %s): this sender only accepts templates, "
                                  "so interactive replies cannot be delivered", contact, result.error_code)
                    else:
                        log.error("whatsapp reply to %s could not be sent (error %s) — check the %s configuration",
                                  contact, result.error_code, self.provider.name)
                    if session_id:
                        for pending in replies[index:]:
                            db.requeue_message(session_id, pending)
                    return False
        return True

    # kept for callers of the Twilio-only channel
    def _push_replies(self, contact: str, replies: list[str]) -> None:
        self.deliver(contact, replies)

    def send_outbound(self, state: SessionState, text: str, purpose: str = "nudge") -> bool:
        """Push a business-initiated message. True if the candidate gets ``text`` (or, for a nudge,
        the template that stands in for it).

        WhatsApp only allows free-form text inside the 24h window opened by the
        candidate's last WhatsApp message. When we know the window is closed the
        template goes straight away; when we don't, free-form is tried first and
        the template is the fallback. A recruiter message or a check decision is
        not replaced by the template: it stays queued and goes out when the
        candidate answers the template.
        """
        address = self.address_of(state)
        if not address:
            return False
        window = self.window_open(state)
        if window is False:
            reason = "window_closed"
        else:
            result = self.provider.send_text(address, self._labelled(state, text))
            self._record(state.id, address, purpose, text, result)
            if result.ok:
                log.info("whatsapp outbound sent session=%s id=%s", state.id, result.id)
                if purpose != "nudge":  # the candidate's answer to this message belongs to this application
                    identity.note_channel(state.candidate_id, self.source, focus=state.id)
                return True
            if result.error_code not in self.provider.window_error_codes:
                return False
            reason = f"error_{result.error_code}"
        return self._send_template(state, reason) and purpose == "nudge"

    def address_of(self, state: SessionState) -> Optional[str]:
        """Where to write to the candidate of ``state`` on this channel: the address the channel last
        saw them use (e.g. WhatsApp's ``+521…`` for a Mexican referral stored as ``+52…``), for this
        or another of their applications; else the application's own number if it was used here."""
        seen = identity.channel_info(state.candidate_id, self.source).get("address")
        if seen:
            return seen
        return state.contact if state.reachable_on(self.source) and state.contact else None

    def can_reach(self, state: SessionState) -> bool:
        return bool(self.address_of(state))

    def _labelled(self, state: SessionState, text: str) -> str:
        """With several live applications, a push says which job it is about."""
        if not identity.live_applications(state.candidate_id, exclude=state.id):
            return text
        try:
            profile = self.agent.profile(state)
        except KeyError:
            return text
        role = localize_text(profile.role, state.language.value)
        label = " · ".join(part for part in (role, profile.company) if part)
        return f"{label}: {text}" if label else text

    def send_message(self, state: SessionState, text: str) -> bool:
        """Recruiter message / check decision: delivered only if the candidate reads these words.
        Waits for a turn of the candidate that is still being answered, so it comes after that reply
        (never called from the event loop: nudges, which are, do not wait)."""
        address = self.address_of(state)
        if address and not self.wait_until_idle(address):
            log.warning("session %s: a turn is still in flight; sending anyway", state.id)
        return self.send_outbound(state, text, purpose="message")

    def _send_template(self, state: SessionState, reason: str) -> bool:
        if not self.provider.template_configured():
            log.error("session %s: WhatsApp requires a template for this push; set %s",
                      state.id, self.provider.template_setting)
            return False
        address = self.address_of(state)
        result = self.provider.send_template(address, self._template_variables(state), state.language.value)
        self._record(state.id, address, "template", self.provider.template_ref, result)
        if not result.ok:
            return False
        log.info("whatsapp outbound sent as template session=%s id=%s (%s)", state.id, result.id, reason)
        payload = {"template": self.provider.template_ref, "reason": reason, "provider": self.provider.name}
        db.append_events(state.id, [{"type": f"{self.source}_template_fallback", "payload": payload}], state.stage)
        return True

    def _record(self, session_id: Optional[str], contact: str, kind: str, body: str, result: SendResult) -> None:
        try:
            db.record_delivery(session_id, self.source, self.provider.name, result.id, contact, kind, body,
                               "accepted" if result.ok else "failed", result.error_code, result.error)
        except Exception:  # the registry must never block a delivery
            log.exception("could not record whatsapp delivery")
            return
        if result.id:
            with self._seen_lock:
                early = self._early_receipts.pop((self.provider.name, result.id), [])
            for update in early:
                self.on_status(update)

    def on_status(self, update: StatusUpdate) -> None:
        """Provider delivery receipt (Twilio status callback, Meta `statuses` webhook)."""
        status = "failed" if update.status == "undelivered" else update.status  # one vocabulary for both
        row = db.update_delivery(update.provider, update.external_id, status, update.error_code, update.error_detail)
        if row is None:  # the send call has not returned yet: keep it for when it does
            with self._seen_lock:
                self._early_receipts.setdefault((update.provider, update.external_id), []).append(update)
                while len(self._early_receipts) > 1000:
                    self._early_receipts.popitem(last=False)
            return
        if status != "failed" or not row["applied"]:  # not a failure, or already handled by another receipt
            return
        session_id = row["session_id"]
        if not session_id:
            return
        db.append_events(session_id, [{"type": f"{self.source}_delivery_failed", "payload": {
            "kind": row["kind"], "error_code": update.error_code, "detail": update.error_detail,
            "provider": update.provider}}], None)
        log.warning("whatsapp %s %s to session %s failed after being accepted (error %s)",
                    row["kind"], update.external_id, session_id, update.error_code)
        if row["kind"] == "template":
            return
        if row["kind"] != "nudge":  # a nudge is replaced by the template; anything else must still arrive
            db.requeue_message(session_id, row["body"])  # it goes out with the candidate's next message
        state = self.store.get(session_id)
        if state is None or update.error_code not in self.provider.window_error_codes:
            return
        with self.store.lock(session_id):
            self.set_last_inbound(state, datetime.now(timezone.utc) - self.WINDOW - timedelta(hours=1))  # now we know
            db.save_session(state)
            if not self._template_sent_recently(session_id):
                self._send_template(state, f"async_error_{update.error_code}")

    @staticmethod
    def _template_sent_recently(session_id: str, seconds: int = 120) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        return any(d["kind"] == "template" and d["status"] != "failed" and d["created_at"] >= cutoff
                   for d in db.deliveries(session_id))

    def _template_variables(self, state: SessionState) -> dict[str, str]:
        """{{1}} = candidate first name (or a neutral fallback), {{2}} = agent name."""
        profile = self.agent.profile(state)
        name_field = profile.field_of_type("name") or "full_name"
        name = str(state.data.get(name_field) or "").strip().split(" ")[0]
        return {"1": name or ("there" if state.language.value == "en" else "hola"), "2": profile.persona.agent_name}


def _localize(messages: Optional[dict[str, str]], lang: str) -> str:
    if not messages:
        return ""
    return messages.get(lang) or messages["es"]

