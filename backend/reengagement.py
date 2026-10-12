from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .screening.agent import SessionState
from .screening.prompts import REENGAGE_MESSAGES
from .screening.schema import Stage, TurnDecision
from .storage import db

log = logging.getLogger("sazon.reengage")

TICK_SECONDS = 15


class ReengagementMonitor:
    """Background loop that nudges silent candidates and closes dead sessions.

    Nudges are stored as undelivered assistant messages; the frontend fetches
    them via GET /api/sessions/{id}/pending, so re-engagement works even if the
    candidate closed the tab.
    """

    def __init__(self, agent: Any, registry: dict[str, SessionState]):
        self.agent = agent
        self.registry = registry
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
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.tick()
            except Exception:
                log.exception("re-engagement tick failed")

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        for state in list(self.registry.values()):
            if state.status != "active" or state.stage in (Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED):
                continue
            if not any(m.get("role") == "user" for m in state.history):
                continue
            idle = (now - state.last_candidate_ts).total_seconds()
            attempts = state.reengage_attempts
            if attempts == 0 and idle >= settings.reengage_first_delay:
                self._nudge(state, 1)
            elif attempts == 1 and idle >= settings.reengage_second_delay:
                self._nudge(state, 2)
            elif attempts >= settings.reengage_max_attempts and idle >= settings.session_idle_seconds:
                self._close_abandoned(state)

    def _nudge(self, state: SessionState, attempt: int) -> None:
        messages = REENGAGE_MESSAGES.get(attempt) or REENGAGE_MESSAGES[1]
        text = messages.get(state.language.value) or messages["es"]
        state.reengage_attempts = attempt
        state.history.append({"role": "assistant", "content": text})
        db.append_message(state.id, "assistant", text, state.stage.value, delivered=False)
        db.set_reengage_attempts(state.id, attempt)
        db.append_events(state.id, [{"type": "reengage_nudge", "payload": {"attempt": attempt, "idle_seconds": int((datetime.now(timezone.utc) - state.last_candidate_ts).total_seconds())}}], state.stage.value)
        log.info("re-engagement nudge %d for session %s", attempt, state.id)

    def _close_abandoned(self, state: SessionState) -> None:
        decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
        self.agent._close(state, decision, Stage.ABANDONED, "silence", None)
        db.save_session(state)
        db.append_events(state.id, decision.events, state.stage.value)
        log.info("session %s closed as abandoned after silence", state.id)
