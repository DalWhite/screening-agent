"""The web chat as a channel: sessions addressed by id, replies in the HTTP answer, pushes by polling.

Its transport is our own session REST API (``backend/api/sessions.py``), used by
the browser chat and by integrations that relay their own channel (an ATS, an
SMS gateway: ``source="ats"``...). Conversation logic only: no HTTP here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import settings
from ..screening.agent import SessionState
from ..screening.profile import registry as profile_registry
from ..screening.schema import FINAL_STAGES, Stage, TERMINAL_STAGES
from ..storage import db
from .base import BaseChannel, persist_turn


class WebChannelError(Exception):
    """A request the web channel cannot serve (the API maps each to an HTTP status)."""


class SessionNotFound(WebChannelError):
    pass


class UnknownProcess(WebChannelError):
    pass


class ResumeWindowExpired(WebChannelError):
    pass


class DecisionIsFinal(WebChannelError):
    pass


# Channels with their own transport: a session that started there and is continued through the session
# API (e.g. a referral opening the link) is on the web chat; any other source is an API integration
# relaying its own channel (an ATS...), recorded under its own name.
_NATIVE = {"web", "backoffice", "whatsapp", "voice"}


class WebChannel(BaseChannel):
    source = "web"

    def channel_of(self, state: SessionState) -> str:
        return state.source if state.source not in _NATIVE else self.source

    def session(self, session_id: str) -> SessionState:
        state = self.store.get(session_id)
        if state is None:
            raise SessionNotFound(session_id)
        return state

    # ---------- conversation ----------

    def start(self, source: str = "web", language: Optional[str] = None,
              profile_id: Optional[str] = None) -> tuple[SessionState, str]:
        """A new screening and its opening message."""
        profiles = profile_registry()
        profiles.refresh_if_changed()
        if profile_id and (profile_id not in profiles or not profiles.get(profile_id).active):
            raise UnknownProcess(profile_id)
        state = self.agent.new_session(source=source, language=language, profile_id=profile_id or None)
        reply = self.agent.opening(state)
        self.store.put(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, Stage.INTRO, channel=self.channel_of(state))
        return state, reply

    def message(self, session_id: str, text: str) -> dict[str, Any]:
        """One candidate turn (reopening an abandoned screening inside the resume window)."""
        state = self.session(session_id)
        with self.store.lock(state.id):
            resume_reply = self._resume_if_needed(state)
            channel = self.channel_of(state)
            previous = state.use_channel(channel)
            if previous:  # e.g. a candidate registered by a recruiter opens the screening link
                db.append_events(state.id, [{"type": "channel_switch",
                                             "payload": {"from": previous, "to": channel, "stage": state.stage}}],
                                 state.stage)
            reply, decision = self.agent.process(state, text)
            persist_turn(state, text, reply, decision, channel=channel)
        return {
            "reply": reply,
            "paused": decision.paused,
            "bot_paused": state.bot_paused,
            "stage": state.stage,
            "status": state.status,
            "language": state.language.value,
            "advanced": decision.advanced,
            "validation_error": decision.validation_error,
            "guardrail": decision.guardrail,
            "rag_used": decision.rag_used,
            "resumed": resume_reply is not None,
            "resume_reply": resume_reply,
            "summary": state.summary,
        }

    def pending(self, session_id: str) -> dict[str, Any]:
        """What was queued for the candidate (nudges, recruiter messages, decisions) + live status:
        the web chat polls this, so delivery happens here."""
        state = self.session(session_id)
        return {
            "messages": db.pending_messages(session_id, channel=self.channel_of(state)),
            "session": {"status": state.status, "stage": state.stage, "bot_paused": state.bot_paused,
                        "language": state.language.value},
        }

    def resume(self, session_id: str) -> dict[str, Any]:
        state = self.session(session_id)
        if state.stage in FINAL_STAGES or state.stage == Stage.VERIFYING:
            raise DecisionIsFinal(session_id)
        if state.status == "active":
            reply = self.agent.resume(state)
            db.save_session(state)
            db.append_message(state.id, "assistant", reply, state.stage, channel=self.channel_of(state))
            return {"reply": reply, "stage": state.stage, "status": state.status, "language": state.language.value}
        resume_reply = self._resume_if_needed(state)
        return {"reply": resume_reply or self.agent.resume(state), "stage": state.stage, "status": state.status,
                "language": state.language.value}

    def end(self, session_id: str) -> dict[str, Any]:
        """The candidate leaves early: closed as qualified if every answer is in, abandoned otherwise."""
        state = self.session(session_id)
        if state.stage in TERMINAL_STAGES:
            return {"reply": "", "status": state.status}
        reply, decision = self.agent.end(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, state.stage, channel=self.channel_of(state))
        db.append_events(state.id, decision.events, state.stage)
        return {"reply": reply, "status": state.status, "summary": state.summary}

    def _resume_if_needed(self, state: SessionState) -> Optional[str]:
        """Reopen an abandoned session when the candidate comes back in time."""
        if state.status != "abandoned" or state.bot_paused:
            return None
        if datetime.now(timezone.utc) - state.last_candidate_ts > timedelta(hours=settings.resume_window_hours):
            raise ResumeWindowExpired(state.id)
        reply = self.agent.resume(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, state.stage, channel=self.channel_of(state))
        db.append_events(state.id, [{"type": "session_resume", "payload": {"stage": state.stage}}], state.stage)
        return reply
