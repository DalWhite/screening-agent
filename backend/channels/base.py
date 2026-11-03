from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.guardrails import redact_pii, screen_input
from ..screening.schema import Lang, Stage, TERMINAL_STAGES, TurnDecision
from ..session_store import SessionStore
from ..storage import db

FINAL_STAGES = {Stage.QUALIFIED, Stage.DISQUALIFIED}

# BCP-47 locales Twilio understands for <Say>/<Gather input="speech">.
VOICE_LOCALES: dict[str, str] = {"es": "es-ES", "en": "en-US"}

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\u2190-\u21ff\u2700-\u27bf\ufe0f]"
)
_MARKDOWN_RE = re.compile(r"[*_`#>]+")


def normalize_contact(raw: str) -> str:
    """`whatsapp:+34600111222` -> `+34600111222` (stable identity across channels)."""
    return (raw or "").strip().removeprefix("whatsapp:").replace(" ", "")


def voice_locale(language: Lang) -> str:
    return VOICE_LOCALES.get(language.value, VOICE_LOCALES["es"])


def strip_for_voice(text: str) -> str:
    """Make chat-ish text speakable: no emojis, no markdown, single spaces."""
    clean = _EMOJI_RE.sub("", text)
    clean = _MARKDOWN_RE.sub("", clean)
    return re.sub(r"\s+", " ", clean).strip()


def split_message(text: str, limit: int) -> list[str]:
    """Split long outbound text on sentence/word boundaries (WhatsApp caps body size)."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


@dataclass
class ResolvedSession:
    """A channel turn's session plus any greeting the channel must emit first."""

    state: SessionState
    greetings: list[str] = field(default_factory=list)
    created: bool = False
    resumed: bool = False


class BaseChannel:
    """Shared behaviour for phone-number-addressed channels (WhatsApp, voice).

    Maps a contact (E.164 number) to a screening session with the same
    lifecycle rules as the web chat: active sessions continue, abandoned ones
    resume inside `RESUME_WINDOW_HOURS`, final decisions stay final and a new
    conversation starts instead.
    """

    source = "channel"

    def __init__(self, agent: ScreeningAgent, store: SessionStore):
        self.agent = agent
        self.store = store

    # ---------- session lifecycle ----------

    def resolve_session(self, contact: str, language: Optional[str] = None) -> ResolvedSession:
        contact = normalize_contact(contact)
        state = self.store.find_by_contact(self.source, contact) if contact else None

        if state is not None and state.status == "active" and state.stage not in TERMINAL_STAGES:
            return ResolvedSession(state=state)

        if state is not None and state.status == "abandoned" and state.stage not in FINAL_STAGES:
            age = datetime.now(timezone.utc) - state.last_candidate_ts
            if age <= timedelta(hours=settings.resume_window_hours):
                reply = self.agent.resume(state)
                db.save_session(state)
                db.append_message(state.id, "assistant", reply, state.stage.value)
                db.append_events(
                    state.id,
                    [{"type": "session_resume", "payload": {"stage": state.stage.value, "channel": self.source}}],
                    state.stage.value,
                )
                return ResolvedSession(state=state, greetings=[reply], resumed=True)

        return self._new_session(contact, language)

    def _new_session(self, contact: str, language: Optional[str]) -> ResolvedSession:
        state = self.agent.new_session(source=self.source, language=language, contact=contact)
        opening = self.agent.opening(state)
        self.store.put(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", opening, Stage.INTRO.value)
        db.append_events(
            state.id,
            [{"type": "channel_session_started", "payload": {"channel": self.source}}],
            Stage.INTRO.value,
        )
        return ResolvedSession(state=state, greetings=[opening], created=True)

    # ---------- turns ----------

    def run_turn(self, state: SessionState, text: str) -> tuple[str, TurnDecision]:
        """One agent turn + persistence, identical to the web chat's pipeline."""
        reply, decision = self.agent.process(state, text)
        clean = redact_pii(screen_input(text).sanitized)
        db.save_session(state)
        db.append_message(state.id, "user", clean, decision.stage_before.value)
        db.append_message(state.id, "assistant", reply, decision.stage_after.value)
        db.append_events(state.id, decision.events, decision.stage_after.value)
        return reply, decision

    def pending_nudges(self, state: SessionState) -> list[str]:
        """Undelivered re-engagement nudges queued while the candidate was away."""
        return [m["content"] for m in db.pending_messages(state.id)]
