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
from ..identity import service as identity

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
    follow_up: bool = False  # the candidate got back to us about a decided application


class BaseChannel:
    """Shared behaviour for phone-number-addressed channels (WhatsApp, voice).

    Maps a phone number to the *candidate* behind it, then to the application
    (session) the message belongs to (docs/identity_design.md):

    1. an application a recruiter is talking to the candidate in;
    2. the application the conversation on this channel is about (its focus);
    3. the line's process: an active application continues (unless it went
       stale), an abandoned one resumes inside `RESUME_WINDOW_HOURS`;
    4. a decision of the last `FOLLOW_UP_DAYS`: the message follows up on it
       (a decided candidate is never screened again from scratch);
    5. otherwise a new application of the same candidate.

    Only applications reached through this same phone line are continued; the
    candidate's other applications are linked, never taken over.
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
        """Find (or open) the candidate's application. With ``store_greetings=False`` the caller
        stores the greetings itself, after the candidate's message, so the transcript keeps
        the real order (the candidate wrote first, then the agent greeted)."""
        address = normalize_contact(contact)
        key = identity.phone_key(address)
        if key is None:  # no number, or a withheld one: nobody to recognise, nobody to mix up
            resolved = self._new_session(None, language, candidate_id=None)
        else:
            with identity.lock(key):
                candidate_id = identity.candidate_for_phone(key, verified=True, source=self.source)
                resolved = None
                for state, mode in self._applications_of(candidate_id, key):
                    resolved = self._continue(state, mode)
                    if resolved is not None:
                        resolved = self._arrive(resolved)
                        break
                if resolved is None:
                    resolved = self._new_session(address, language, candidate_id)
                identity.note_channel(candidate_id, self.source, address=address, focus=resolved.state.id)
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

    def _line_profile(self) -> Optional[str]:
        try:
            return self.agent.profiles.get(self.profile_id).id
        except KeyError:
            return None

    def _process_active(self, profile_id: str) -> bool:
        try:
            return bool(self.agent.profiles.get(profile_id).active)
        except KeyError:
            return False

    def _follow_up_days(self, state: SessionState) -> int:
        try:
            return identity.follow_up_days(self.agent.profile(state))
        except KeyError:
            return settings.follow_up_days

    def _applications_of(self, candidate_id: str, key: str) -> Iterator[tuple[SessionState, str]]:
        """The candidate's applications this message could belong to, in order of preference,
        with how to treat each: ``recruiter`` / ``live`` / ``follow_up``."""
        rows = [r for r in identity.applications(candidate_id) if identity.phone_key(r["contact"]) == key]
        updated = {r["id"]: r["updated_at"] for r in rows}
        states = [s for s in (self.store.get(r["id"]) for r in rows) if s is not None]
        for state in states:  # a recruiter is talking to the candidate: their answer goes there
            if state.bot_paused and identity.recently(updated.get(state.id), self._follow_up_days(state)):
                yield state, "recruiter"
        focus_id = identity.channel_info(candidate_id, self.source).get("focus")
        focus = next((s for s in states if s.id == focus_id), None)
        if focus is not None and self._process_active(focus.profile_id):
            yield focus, "live"
        line = self._line_profile()
        own = [s for s in states if s.profile_id == line]
        if line and self._process_active(line):
            for state in own:
                yield state, "live"
        for state in ([focus] if focus is not None else []) + own:
            yield state, "follow_up"

    def _continue(self, state: SessionState, mode: str = "live") -> Optional[ResolvedSession]:
        """Lifecycle rules (None: not this one). Active applications continue, abandoned ones resume
        inside the window, decisions get a follow-up for a while and then stay closed."""
        if mode == "recruiter":
            return ResolvedSession(state=state)
        if mode == "follow_up":
            if state.stage in FINAL_STAGES and identity.recently(self._decided_at(state), self._follow_up_days(state)):
                return ResolvedSession(state=state, follow_up=True)
            return None
        if state.status == "active" and state.stage not in TERMINAL_STAGES:
            if self._stale(state):
                self._expire(state)
                return None
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

    @staticmethod
    def _decided_at(state: SessionState) -> Optional[str]:
        return (state.summary or {}).get("generated_at") or state.last_candidate_ts.isoformat()

    @staticmethod
    def _stale(state: SessionState) -> bool:
        """An "active" application nobody touched for longer than anyone could be expected to come back
        to it (the silence monitor normally closes them within minutes)."""
        if state.bot_paused:
            return False  # a recruiter owns the conversation, however long it takes
        never_answered = not any(m.get("role") == "user" for m in state.history)
        limit = (timedelta(days=settings.referral_wait_days) if never_answered and state.source == "backoffice"
                 else timedelta(hours=settings.resume_window_hours))
        return datetime.now(timezone.utc) - state.last_candidate_ts > limit

    def _expire(self, state: SessionState) -> None:
        _, decision = self.agent.end(state, reason="expired")
        db.save_session(state)
        db.append_events(state.id, decision.events, state.stage)
        log.info("session %s expired (stale), a new application starts", state.id)

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

    def _new_session(self, contact: Optional[str], language: Optional[str],
                     candidate_id: Optional[str] = None) -> ResolvedSession:
        state = self.agent.new_session(source=self.source, language=language, contact=contact,
                                       profile_id=self.profile_id)
        state.candidate_id = candidate_id
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
            reply = link_identity(self.agent, state, decision, reply)
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


DUPLICATE_NOTICE = {
    "es": "Ya tenemos una candidatura tuya con este documento para este puesto, así que no abro otra. "
          "Un reclutador la revisará y se pondrá en contacto contigo.",
    "en": "We already have an application of yours with this document for this job, so I won't open another one. "
          "A recruiter will review it and get in touch.",
}


def link_identity(agent: ScreeningAgent, state: SessionState, decision: TurnDecision, reply: str) -> str:
    """An ID document typed in this turn is an identity key. If another candidate already has it, both are
    the same person: merged. If that person already has a live application in this process, this one
    closes as a duplicate (the recruiter reviews it) and the reply says so. Returns the reply to send."""
    try:
        profile = agent.profile(state)
    except KeyError:
        return reply
    numbers = [decision.secrets[q.field] for q in profile.questions
               if q.type == "id_document" and decision.secrets.get(q.field)]
    if not numbers:
        return reply
    if not state.candidate_id:
        db.save_session(state)  # storage assigns the application's candidate
    for number in numbers:
        match = identity.match_document(state, number)
        if match is None:
            continue
        if match.live_duplicate:
            notice = DUPLICATE_NOTICE.get(state.language.value) or DUPLICATE_NOTICE["es"]
            if state.history and state.history[-1].get("role") == "assistant" and state.history[-1].get("content") == reply:
                state.history.pop()  # the question that would have followed is not asked any more
            reply = agent._close(state, decision, Stage.DUPLICATE, "duplicate_application", None, override=notice)
            decision.check_requests.clear()  # nothing to verify on a duplicate
            db.save_session(state)  # closed before the merge: one live application per candidate and process
        moved = identity.merge_into(state, match.owner)
        decision.events.append({"type": "identity_merge", "payload": {
            "key": "id_document", "into": match.owner, "applications": len(moved),
            "duplicate_of": match.live_duplicate}})
        log.info("session %s: same ID document as candidate %s (merged, duplicate_of=%s)",
                 state.id, match.owner, match.live_duplicate)
    return reply


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
