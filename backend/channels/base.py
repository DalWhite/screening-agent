from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.guardrails import redact_pii, screen_input
from ..screening.schema import FINAL_STAGES, Lang, Stage, TERMINAL_STAGES, TurnDecision
from ..session_store import SessionStore
from ..storage import db
from .. import verifications

log = logging.getLogger("sazon.channels")

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
    switched: bool = False  # the candidate came from another channel (same number, same process)
    previous_channel: Optional[str] = None
    first_contact: bool = False  # a screening someone else opened (recruiter) that the candidate never answered
    stored: bool = False  # greetings already in the transcript


class BaseChannel:
    """Shared behaviour for phone-number-addressed channels (WhatsApp, voice).

    Maps a contact (E.164 number) to a screening session with the same
    lifecycle rules as the web chat: active sessions continue, abandoned ones
    resume inside `RESUME_WINDOW_HOURS`, final decisions stay final and a new
    conversation starts instead. The session may have started on another
    channel of the same process (channel continuity, see `_sessions_of`).
    """

    source = "channel"

    def __init__(self, agent: ScreeningAgent, store: SessionStore, profile_id: Optional[str] = None):
        self.agent = agent
        self.store = store
        # A phone number reaches one recruiting process (WHATSAPP_PROFILE / VOICE_PROFILE).
        self.profile_id = profile_id or None

    # ---------- session lifecycle ----------

    def resolve_session(self, contact: str, language: Optional[str] = None,
                        store_greetings: bool = True) -> ResolvedSession:
        """Find (or open) the candidate's screening. With ``store_greetings=False`` the caller
        stores the greetings itself, after the candidate's message, so the transcript keeps
        the real order (the candidate wrote first, then the agent greeted)."""
        contact = normalize_contact(contact)
        resolved = None
        if contact:
            for state in self._sessions_of(contact):
                resolved = self._continue(state)
                if resolved is not None:
                    resolved = self._arrive(resolved)
                    break
        if resolved is None:
            resolved = self._new_session(contact, language)
        if store_greetings:
            self.store_greetings(resolved)
        return resolved

    def store_greetings(self, resolved: ResolvedSession) -> None:
        if resolved.stored:
            return
        resolved.stored = True
        state = resolved.state
        for text in resolved.greetings:
            db.append_message(state.id, "assistant", text, state.stage, channel=self.source)
        db.save_session(state)

    def discard_greetings(self, resolved: ResolvedSession) -> None:
        """The candidate's message already carries an answer: don't re-ask, let the turn reply."""
        state = resolved.state
        for text in resolved.greetings:
            for i in range(len(state.history) - 1, -1, -1):
                if state.history[i].get("role") == "assistant" and state.history[i].get("content") == text:
                    del state.history[i]
                    break
        resolved.greetings = []
        resolved.stored = True

    def _sessions_of(self, contact: str) -> Iterator[SessionState]:
        """Sessions this contact could be continuing, in order of preference.

        This channel's latest session first (the historical rule), then undecided
        sessions of the same process on other channels: the candidate who
        started on WhatsApp and now calls, or who was registered by a recruiter.
        """
        own = self.store.find_by_contact(self.source, contact)
        if own is not None:
            yield own
        try:
            profile_id = self.agent.profiles.get(self.profile_id).id
        except KeyError:
            return
        for state in self.store.find_live_by_contact(contact, profile_id):
            if own is None or state.id != own.id:
                yield state

    def _continue(self, state: SessionState) -> Optional[ResolvedSession]:
        """Lifecycle rules: active sessions continue, abandoned ones resume inside the window,
        final decisions stay final (None: start a new conversation)."""
        if state.status == "active" and state.stage not in TERMINAL_STAGES:
            return ResolvedSession(state=state)
        if state.status == Stage.VERIFYING:
            return ResolvedSession(state=state)  # decision pending external checks: same conversation

        if state.status == "abandoned" and state.stage not in FINAL_STAGES:
            age = datetime.now(timezone.utc) - state.last_candidate_ts
            if age <= timedelta(hours=settings.resume_window_hours):
                reply = self.agent.resume(state)
                db.save_session(state)
                db.append_events(
                    state.id,
                    [{"type": "session_resume", "payload": {"stage": state.stage, "channel": self.source}}],
                    state.stage,
                )
                return ResolvedSession(state=state, greetings=[reply], resumed=True)
        return None

    def _arrive(self, resolved: ResolvedSession) -> ResolvedSession:
        """The candidate reached us on this channel: record a channel switch and greet them.

        After a switch the candidate cannot see where the conversation stopped
        (it happened on another channel), so the agent says hello again and
        re-asks the pending question — or, for a screening a recruiter opened
        and the candidate never answered, starts with the opening.
        """
        state = resolved.state
        previous = state.use_channel(self.source)
        if previous is None:
            return resolved
        resolved.switched = True
        resolved.previous_channel = previous
        db.append_events(state.id, [{"type": "channel_switch",
                                     "payload": {"from": previous, "to": self.source, "stage": state.stage}}],
                         state.stage)
        if not resolved.greetings and not state.bot_paused and state.stage not in TERMINAL_STAGES:
            if not any(m.get("role") == "user" for m in state.history):
                greeting = self.agent.opening(state)
                resolved.first_contact = True
            else:
                greeting = f"{self.agent.resume_text(state)} {self.agent.question_text(state)}".strip()
                state.history.append({"role": "assistant", "content": greeting})
            resolved.greetings = [greeting]
        db.save_session(state)
        log.info("session %s switched channel %s -> %s at %s", state.id, previous, self.source, state.stage)
        return resolved

    def _new_session(self, contact: str, language: Optional[str]) -> ResolvedSession:
        state = self.agent.new_session(source=self.source, language=language, contact=contact,
                                       profile_id=self.profile_id)
        opening = self.agent.opening(state)
        self.store.put(state)
        db.save_session(state)
        db.append_events(
            state.id,
            [{"type": "channel_session_started", "payload": {"channel": self.source}}],
            Stage.INTRO,
        )
        return ResolvedSession(state=state, greetings=[opening], created=True)

    # ---------- turns ----------

    def run_turn(self, state: SessionState, text: str, kind: str = "text", meta: Optional[dict] = None,
                 preface: tuple[str, ...] = ()) -> tuple[str, TurnDecision]:
        """One agent turn + persistence, identical to the web chat's pipeline."""
        with self.store.lock(state.id):
            reply, decision = self.agent.process(state, text)
            persist_turn(state, text, reply, decision, channel=self.source, kind=kind, meta=meta, preface=preface)
        return reply, decision

    def record_assistant(self, state: SessionState, text: str, kind: str = "text",
                         meta: Optional[dict] = None) -> None:
        """Something the agent said outside a turn (re-prompt, notice): the transcript keeps everything."""
        state.history.append({"role": "assistant", "content": text})
        db.append_message(state.id, "assistant", text, state.stage, channel=self.source, kind=kind, meta=meta)

    def pending_nudges(self, state: SessionState) -> list[str]:
        """Undelivered messages (recruiter messages, check decisions) queued while the candidate was away.
        Undelivered re-engagement nudges are dropped: the candidate just came back."""
        return [m["content"] for m in db.pending_messages(state.id, channel=self.source, supersede=("nudge",))]


def persist_turn(state: SessionState, user_text: str, reply: str, decision: TurnDecision,
                 channel: Optional[str] = None, kind: str = "text", meta: Optional[dict] = None,
                 preface: tuple[str, ...] = ()) -> None:
    """Store one candidate turn (same pipeline for web, WhatsApp and voice).

    ``channel`` / ``kind`` / ``meta`` describe how the candidate said it (a typed WhatsApp
    message, speech on a call with its STT confidence, a transcribed voice note...).
    ``preface`` are assistant lines sent before the reply (e.g. "welcome back").
    During a human takeover the agent stays silent: only the candidate message is stored.
    """
    channel = channel or state.current_channel
    clean = redact_pii(screen_input(user_text).sanitized)
    db.save_session(state)
    verifications.dispatch_requests(state, decision)  # external checks: queued now, answered asynchronously
    db.append_message(state.id, "user", clean, decision.stage_before, channel=channel, kind=kind, meta=meta)
    for text in preface:
        db.append_message(state.id, "assistant", text, decision.stage_before, channel=channel, kind="notice")
    if reply:
        db.append_message(state.id, "assistant", reply, decision.stage_after, channel=channel)
    db.append_events(state.id, decision.events, decision.stage_after)
