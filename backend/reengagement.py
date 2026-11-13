from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import settings
from .screening.agent import SessionState
from .screening.schema import TERMINAL_STAGES
from .storage import db

log = logging.getLogger("sazon.reengage")

TICK_SECONDS = 15  # default of REENGAGE_TICK_SECONDS


class ReengagementMonitor:
    """Background loop that nudges silent candidates and closes dead sessions.

    Nudges are stored as undelivered assistant messages; the frontend fetches
    them via GET /api/sessions/{id}/pending, so re-engagement works even if the
    candidate closed the tab. Push channels (WhatsApp) register a `notifier`
    callback: when it reports the nudge as delivered, the message is stored as
    delivered instead of queued.
    """

    def __init__(self, agent: Any, registry: dict[str, SessionState],
                 notifier: Optional[Callable[[SessionState, str], bool]] = None):
        self.agent = agent
        self.registry = registry
        self.notifier = notifier
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.reengage_tick_seconds or TICK_SECONDS)
            try:
                self.tick()
            except Exception:
                log.exception("re-engagement tick failed")

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        for state in list(self.registry.values()):
            if state.status != "active" or state.stage in TERMINAL_STAGES:
                continue
            if state.bot_paused:  # a recruiter owns the conversation: no automatic nudges or closing
                continue
            idle = (now - state.last_candidate_ts).total_seconds()
            if not any(m.get("role") == "user" for m in state.history):
                # never answered (a caller who hung up during the opening, an ignored referral): not
                # nudged, but not "in conversation" for ever either
                if idle >= self._never_answered_limit(state):
                    self._close(state, reason="expired")
                continue
            attempts = state.reengage_attempts
            if attempts == 0 and idle >= settings.reengage_first_delay:
                self._nudge(state, 1)
            elif attempts == 1 and idle >= settings.reengage_second_delay:
                self._nudge(state, 2)
            elif attempts >= settings.reengage_max_attempts and idle >= settings.session_idle_seconds:
                self._close_abandoned(state)

    def _nudge(self, state: SessionState, attempt: int) -> None:
        text = self.agent.reengage_text(state, attempt)
        state.reengage_attempts = attempt
        state.history.append({"role": "assistant", "content": text})
        notifier = self.notifier
        pushed = db.push_message(state.id, "assistant", text, state.stage,
                                 (lambda: notifier(state, text)) if notifier is not None else (lambda: False),
                                 kind="nudge")
        db.set_reengage_attempts(state.id, attempt)
        db.append_events(state.id, [{"type": "reengage_nudge", "payload": {"attempt": attempt, "pushed": pushed, "idle_seconds": int((datetime.now(timezone.utc) - state.last_candidate_ts).total_seconds())}}], state.stage)
        log.info("re-engagement nudge %d for session %s (pushed=%s)", attempt, state.id, pushed)

    @staticmethod
    def _never_answered_limit(state: SessionState) -> float:
        if state.source == "backoffice":  # a recruiter's referral waits longer for the candidate
            return settings.referral_wait_days * 86400
        return settings.resume_window_hours * 3600

    def _close_abandoned(self, state: SessionState) -> None:
        self._close(state, reason="silence")

    def _close(self, state: SessionState, reason: str) -> None:
        _, decision = self.agent.end(state, reason=reason)
        db.save_session(state)
        db.append_events(state.id, decision.events, state.stage)
        log.info("session %s closed as abandoned (%s)", state.id, reason)
