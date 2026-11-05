from __future__ import annotations

import logging
from urllib.parse import quote

from twilio.twiml.voice_response import Gather, VoiceResponse

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.schema import TERMINAL_STAGES
from ..session_store import SessionStore
from ..storage import db
from .base import VOICE_LOCALES, BaseChannel, strip_for_voice, voice_locale

log = logging.getLogger("sazon.voice")

COLLECT_PATH = "/api/channels/voice/collect"

GOODBYE_MESSAGES: dict[str, str] = {
    "es": "No pasa nada. Puedes volver a llamar cuando quieras y seguimos donde lo dejamos. ¡Hasta pronto!",
    "en": "No problem. Call back any time and we'll pick up right where we stopped. Bye for now!",
}

SESSION_LOST_MESSAGES: dict[str, str] = {
    "es": "Lo siento, ha habido un problema técnico. Vuelve a llamar y empezamos de nuevo.",
    "en": "Sorry, something went wrong on our side. Please call again and we'll restart.",
}


class VoiceChannel(BaseChannel):
    """Twilio Programmable Voice adapter.

    The whole call is a TwiML loop: <Say> the agent's reply, <Gather
    input="speech"> the candidate's answer, POST it back to the collect
    webhook, repeat. Twilio does the STT/TTS, the agent only ever sees text —
    the same text pipeline (guardrails, extraction, validation) as every other
    channel.
    """

    source = "voice"

    def __init__(self, agent: ScreeningAgent, store: SessionStore, profile_id: str | None = None):
        super().__init__(agent, store, profile_id)
        self.calls: dict[str, str] = {}  # CallSid -> session_id

    # ---------- webhooks ----------

    def handle_call_start(self, from_addr: str, call_sid: str) -> str:
        """Incoming call: open/resume a session and speak the first prompt."""
        resolved = self.resolve_session(from_addr)
        state = resolved.state
        if call_sid:
            self.calls[call_sid] = state.id

        if resolved.greetings:
            text = " ".join(resolved.greetings)
        else:
            # Candidate called back into a still-active session (dropped call):
            # welcome them back and re-ask the current question.
            text = f"{self.agent.resume_text(state)} {self.agent.question_text(state)}".strip()
            state.history.append({"role": "assistant", "content": text})
            db.save_session(state)
            db.append_message(state.id, "assistant", text, state.stage)

        log.info(
            "voice call start call=%s session=%s created=%s resumed=%s",
            call_sid, state.id, resolved.created, resolved.resumed,
        )
        return self._ask(state, text, retries=0)

    def handle_speech(self, session_id: str, speech: str, retries: int = 0) -> str:
        """Speech-gather callback: run one agent turn and speak the reply."""
        state = self.store.get(session_id)
        if state is None:
            return self._hangup_with(SESSION_LOST_MESSAGES, "es")

        speech = (speech or "").strip()
        if not speech:
            return self._handle_silence(state, retries)

        reply, _ = self.run_turn(state, speech)
        if not reply:  # human takeover: the recruiter answers in writing, tell the caller
            reply = self.agent.paused_text(state)
        if state.stage in TERMINAL_STAGES:
            return self._goodbye(state, reply)
        return self._ask(state, reply, retries=0)

    def handle_call_ended(self, call_sid: str, call_status: str) -> None:
        """Status callback: keep the transcript honest about how the call ended.

        An unfinished session stays active on purpose — the re-engagement
        monitor closes it as abandoned after the idle window, and the candidate
        can call back within RESUME_WINDOW_HOURS to continue.
        """
        session_id = self.calls.pop(call_sid, None)
        if session_id is None:
            return
        state = self.store.get(session_id)
        if state is None:
            return
        db.append_events(
            session_id,
            [{"type": "voice_call_ended", "payload": {"call_sid": call_sid, "status": call_status,
                                                      "stage": state.stage}}],
            state.stage,
        )
        db.save_session(state)
        log.info("voice call ended call=%s session=%s status=%s", call_sid, session_id, call_status)

    # ---------- silence inside a live call ----------

    def _handle_silence(self, state: SessionState, retries: int) -> str:
        if retries < settings.voice_max_silence_retries:
            nudge = self.agent.reengage_text(state, 1)
            question = self.agent.question_text(state)
            db.append_events(
                state.id,
                [{"type": "voice_silence_retry", "payload": {"attempt": retries + 1}}],
                state.stage,
            )
            return self._ask(state, f"{nudge} {question}".strip(), retries=retries + 1)
        reply, decision = self.agent.end(state, reason="silence")
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, state.stage)
        db.append_events(state.id, decision.events, state.stage)
        log.info("voice session %s closed as abandoned after in-call silence", state.id)
        return self._goodbye(state, reply)

    # ---------- TwiML ----------

    def _collect_url(self, state: SessionState, retries: int) -> str:
        base = settings.public_base_url  # empty -> relative URL, resolved by Twilio
        return f"{base}{COLLECT_PATH}?session_id={quote(state.id)}&retries={retries}"

    def _say(self, node, state: SessionState, text: str) -> None:
        kwargs = {"language": voice_locale(state.language)}
        if settings.voice_tts_voice:
            kwargs["voice"] = settings.voice_tts_voice
        node.say(strip_for_voice(text), **kwargs)

    def _ask(self, state: SessionState, text: str, retries: int) -> str:
        response = VoiceResponse()
        gather = Gather(
            input="speech",
            language=voice_locale(state.language),
            action=self._collect_url(state, retries),
            method="POST",
            speech_timeout="auto",
        )
        self._say(gather, state, text)
        response.append(gather)
        # No speech detected: fall through and re-enter collect with an empty result.
        response.redirect(self._collect_url(state, retries), method="POST")
        return str(response)

    def _goodbye(self, state: SessionState, text: str) -> str:
        response = VoiceResponse()
        self._say(response, state, text)
        response.hangup()
        return str(response)

    def _hangup_with(self, messages: dict[str, str], lang: str) -> str:
        response = VoiceResponse()
        locale = VOICE_LOCALES.get(lang, VOICE_LOCALES["es"])
        response.say(messages.get(lang) or messages["es"], language=locale)
        response.hangup()
        return str(response)
