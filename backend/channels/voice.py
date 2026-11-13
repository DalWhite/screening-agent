from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.schema import FINAL_STAGES, TERMINAL_STAGES
from ..session_store import SessionStore
from ..storage import db
from .base import VOICE_LOCALES, BaseChannel, strip_for_voice, voice_locale

log = logging.getLogger("sazon.voice")

GOODBYE_MESSAGES: dict[str, str] = {
    "es": "No pasa nada. Puedes volver a llamar cuando quieras y seguimos donde lo dejamos. ¡Hasta pronto!",
    "en": "No problem. Call back any time and we'll pick up right where we stopped. Bye for now!",
}

# A recruiter owns the conversation (takeover): the call takes a message instead of screening.
TAKEOVER_GREETING: dict[str, str] = {
    "es": "Hola de nuevo. Una persona del equipo de selección está revisando tu candidatura y te contactará pronto. "
          "Si quieres, dime tu mensaje y se lo paso.",
    "en": "Hello again. Someone from the recruiting team is reviewing your application and will contact you soon. "
          "If you like, leave me a message and I'll pass it on.",
}
TAKEOVER_ACK: dict[str, str] = {
    "es": "Gracias, se lo paso al reclutador. ¿Quieres añadir algo más?",
    "en": "Thanks, I'll pass it on to the recruiter. Anything else?",
}
TAKEOVER_GOODBYE: dict[str, str] = {
    "es": "De acuerdo. El equipo de selección te contactará pronto. ¡Hasta luego!",
    "en": "All right. The recruiting team will contact you soon. Bye for now!",
}

# The caller already has a decision (follow-up): no second screening, the call takes a message.
AFTER_DECISION_GREETING: dict[str, str] = {
    "es": "Hola de nuevo. Ya tenemos tu candidatura y el equipo de selección se pondrá en contacto contigo. "
          "Si quieres, dime tu mensaje y se lo paso.",
    "en": "Hello again. We already have your application and the recruiting team will get in touch. "
          "If you like, leave me a message and I'll pass it on.",
}

# Provider retries of the same webhook (same call, same prompt) wait for and replay the first answer.
RETRY_WAIT_SECONDS = 10.0

SESSION_LOST_MESSAGES: dict[str, str] = {
    "es": "Lo siento, ha habido un problema técnico. Vuelve a llamar y empezamos de nuevo.",
    "en": "Sorry, something went wrong on our side. Please call again and we'll restart.",
}


def _float(value: Optional[str]) -> Optional[float]:
    try:
        return round(float(value), 3) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _localized(messages: dict[str, str], state: SessionState) -> str:
    return messages.get(state.language.value) or messages["es"]


@dataclass
class VoiceAction:
    """What the call does next, in provider-neutral terms.

    Voice providers render it (Twilio: TwiML ``<Gather><Say>``; Vonage: an NCCO
    ``talk`` + ``input``): say ``text`` in ``locale``, then either listen for the
    caller's answer (reporting back with ``session_id`` and ``retries``) or hang up.
    """

    text: str
    locale: str
    session_id: Optional[str] = None
    listen: bool = True
    retries: int = 0  # in-call silence re-prompts so far
    # identifies this prompt: adapters put it in the answer URL, so a provider retry of the same
    # answer is recognised (and replayed) instead of being processed twice
    turn: Optional[str] = None

    def to_json(self) -> dict:
        return {"text": self.text, "locale": self.locale, "session_id": self.session_id, "listen": self.listen,
                "retries": self.retries, "turn": self.turn}

    @property
    def hangup(self) -> bool:
        return not self.listen

    @property
    def speech(self) -> str:
        """``text`` made speakable (no emoji / markdown)."""
        return strip_for_voice(self.text)


class VoiceChannel(BaseChannel):
    """Phone screening, independent of the telephony provider.

    A call is a loop of provider events (``start_call`` → ``speech`` … →
    ``call_ended``); each returns a :class:`VoiceAction` that the provider adapter
    renders (``backend/integrations/<vendor>/voice.py``). Speech recognition and synthesis are
    the provider's; the agent only ever sees text — the same pipeline (guardrails,
    extraction, validation) as every other channel.
    """

    source = "voice"

    def __init__(self, agent: ScreeningAgent, store: SessionStore, profile_id: str | None = None):
        super().__init__(agent, store, profile_id)
        self.calls: dict[str, str] = {}  # provider call id -> session_id

    # ---------- provider events ----------

    def start_call(self, from_addr: str, call_id: str) -> VoiceAction:
        """Incoming call: open/resume a session and speak the first prompt. A retry of the same
        call's webhook gets the same answer (one call, one screening)."""
        return self._once("voice:start", call_id, lambda: self._start_call(from_addr, call_id))

    def _once(self, scope: str, key: Optional[str], handle) -> VoiceAction:
        """Process a provider webhook once per ``key``; duplicates wait for the first answer and replay it."""
        if not key:
            return handle()
        deadline = time.monotonic() + RETRY_WAIT_SECONDS
        while not db.claim_idempotency(scope, key):
            done = db.idempotency_result(scope, key)
            if done is not None and done["state"] == "done" and done["response"]:
                log.info("voice %s %s: provider retry answered with the first response", scope, key)
                return VoiceAction(**done["response"])
            if time.monotonic() > deadline:
                log.warning("voice %s %s still being processed after %.0fs", scope, key, RETRY_WAIT_SECONDS)
                return handle()
            time.sleep(0.05)
        try:
            action = handle()
        except Exception:
            db.release_idempotency(scope, key)  # the retry may succeed
            raise
        db.finish_idempotency(scope, key, action.to_json())
        return action

    def _start_call(self, from_addr: str, call_id: str) -> VoiceAction:
        resolved = self.resolve_session(from_addr)
        state = resolved.state
        if call_id:
            self.calls[call_id] = state.id
        db.append_events(state.id, [{"type": "voice_call_started", "payload": {"call_sid": call_id}}], state.stage)

        if state.bot_paused:
            text = _localized(TAKEOVER_GREETING, state)  # no screening question while a recruiter owns it
            self.record_assistant(state, text, kind="notice")
            db.save_session(state)
        elif state.stage in FINAL_STAGES:  # follow-up on a decision: take a message, no new screening
            text = _localized(AFTER_DECISION_GREETING, state)
            self.record_assistant(state, text, kind="notice")
            db.save_session(state)
        elif resolved.greetings:
            text = " ".join(resolved.greetings)
        else:
            # Candidate called back into a still-active session (dropped call):
            # welcome them back and re-ask the current question.
            text = f"{self.agent.resume_text(state)} {self.agent.question_text(state)}".strip()
            self.record_assistant(state, text)
            db.save_session(state)

        log.info("voice call start call=%s session=%s created=%s resumed=%s",
                 call_id, state.id, resolved.created, resolved.resumed)
        return self._ask(state, text, retries=0)

    def speech(self, session_id: str, speech: str, retries: int = 0, confidence: Optional[str] = None,
               call_id: Optional[str] = None, turn: Optional[str] = None) -> VoiceAction:
        """The caller's (transcribed) answer; empty = they said nothing before the timeout. ``turn`` is
        the prompt's token: a provider retry of the same answer is replayed, not processed twice."""
        key = f"{call_id}:{turn}" if call_id and turn else None
        return self._once("voice:turn", key, lambda: self._speech(session_id, speech, retries, confidence, call_id))

    def _speech(self, session_id: str, speech: str, retries: int, confidence: Optional[str],
                call_id: Optional[str]) -> VoiceAction:
        state = self.store.get(session_id)
        if state is None:
            return VoiceAction(_pick(SESSION_LOST_MESSAGES, "es"), VOICE_LOCALES["es"], listen=False)

        speech = (speech or "").strip()
        if not speech:
            return self._handle_silence(state, retries)

        meta = {k: v for k, v in {"call_sid": call_id, "confidence": _float(confidence)}.items() if v is not None}
        reply, _ = self.run_turn(state, speech, kind="speech", meta=meta)
        if not reply:  # human takeover: the message is stored for the recruiter
            reply = _localized(TAKEOVER_ACK, state)
            self.record_assistant(state, reply, kind="notice")
        if state.stage in TERMINAL_STAGES:
            return self._goodbye(state, reply)
        return self._ask(state, reply, retries=0)

    def call_ended(self, call_id: str, status: str) -> None:
        """Keep the transcript honest about how the call ended.

        An unfinished session stays active on purpose — the re-engagement
        monitor closes it as abandoned after the idle window, and the candidate
        can call back within RESUME_WINDOW_HOURS to continue.
        """
        session_id = self.calls.pop(call_id, None)
        if session_id is None and call_id:  # started before a restart: the call is in the events
            session_id = db.session_for_event("voice_call_started", "call_sid", call_id)
        if session_id is None:
            return
        state = self.store.get(session_id)
        if state is None:
            return
        db.append_events(
            session_id,
            [{"type": "voice_call_ended", "payload": {"call_sid": call_id, "status": status, "stage": state.stage}}],
            state.stage,
        )
        db.save_session(state)
        log.info("voice call ended call=%s session=%s status=%s", call_id, session_id, status)

    # ---------- silence inside a live call ----------

    def _handle_silence(self, state: SessionState, retries: int) -> VoiceAction:
        if state.bot_paused:  # nothing to re-ask and nothing to close: the recruiter owns the screening
            goodbye = _localized(TAKEOVER_GOODBYE, state)
            self.record_assistant(state, goodbye, kind="notice")
            return self._goodbye(state, goodbye)
        if retries < settings.voice_max_silence_retries:
            nudge = self.agent.reengage_text(state, 1)
            question = self.agent.question_text(state)
            db.append_events(
                state.id,
                [{"type": "voice_silence_retry", "payload": {"attempt": retries + 1}}],
                state.stage,
            )
            text = f"{nudge} {question}".strip()
            self.record_assistant(state, text, kind="reprompt", meta={"attempt": retries + 1})
            return self._ask(state, text, retries=retries + 1)
        _, decision = self.agent.end(state, reason="silence")
        # The generic closing says a recruiter will review the case; a caller who went
        # silent is told instead that they can call back and carry on (resume window).
        goodbye = _localized(GOODBYE_MESSAGES, state)
        if state.history and state.history[-1].get("role") == "assistant":
            state.history[-1]["content"] = goodbye
        db.save_session(state)
        db.append_message(state.id, "assistant", goodbye, state.stage, channel=self.source)
        db.append_events(state.id, decision.events, state.stage)
        log.info("voice session %s closed as abandoned after in-call silence", state.id)
        return self._goodbye(state, goodbye)

    # ---------- actions ----------

    @staticmethod
    def _ask(state: SessionState, text: str, retries: int) -> VoiceAction:
        return VoiceAction(text, voice_locale(state.language), session_id=state.id, listen=True, retries=retries,
                           turn=uuid.uuid4().hex[:12])

    @staticmethod
    def _goodbye(state: SessionState, text: str) -> VoiceAction:
        return VoiceAction(text, voice_locale(state.language), session_id=state.id, listen=False)


def _pick(messages: dict[str, str], lang: str) -> str:
    return messages.get(lang) or messages["es"]
