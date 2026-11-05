from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from .screening.agent import SessionState
from .screening.schema import CandidateData, Lang
from .storage import db

log = logging.getLogger("sazon.sessions")


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def state_from_row(row: dict) -> Optional[SessionState]:
    """Rehydrate an in-memory SessionState from a stored session row."""
    raw = row.get("data")
    if not isinstance(raw, dict):
        return None
    state = SessionState(
        id=row["id"],
        profile_id=row.get("profile_id") or "",
        profile_revision=row.get("profile_revision"),
        stage=str(row["stage"]),
        data=CandidateData(raw),
        language=Lang(row["language"]),
        status=row["status"],
        source=row["source"],
        contact=row.get("contact"),
        started_at=parse_ts(row["started_at"]),
        reengage_attempts=row["reengage_attempts"],
        flags=list(row.get("flags") or []),
        notes=list(row.get("notes") or []),
        given_up=list(row.get("given_up") or []),
        languages_seen=list(row.get("languages") or []),
        rag_hits=list(row.get("rag_hits") or []),
        summary=row.get("summary"),
        dropped_at=row.get("dropped_at_stage") or (row.get("summary") or {}).get("dropped_at_stage"),
        disqualify_reason=row.get("disqualify_reason"),
        close_reason=(row.get("summary") or {}).get("close_reason"),
        bot_paused=bool(row.get("bot_paused")),
        completed_stages=list(row.get("completed_stages") or (row.get("summary") or {}).get("completed_stages") or []),
        attempts=dict(row.get("attempts") or {}),
        abuse_count=int(row.get("abuse_count") or 0),
    )
    full = db.get_session(row["id"]) or {}
    # recruiter messages are part of the conversation the model must see (as the assistant side)
    state.history = [
        {"role": "user" if m["role"] == "user" else "assistant", "content": m["content"]}
        for m in full.get("messages", [])
    ]
    try:
        state.last_candidate_ts = parse_ts(row["last_ts"])
    except ValueError:
        pass
    return state


class SessionStore:
    """In-memory registry backed by SQLite. Sessions fall out of the registry on
    restart; any lookup transparently rehydrates them from storage, including
    lookups by channel identity (WhatsApp number, caller id)."""

    def __init__(self) -> None:
        self.registry: dict[str, SessionState] = {}
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    def lock(self, session_id: str) -> threading.RLock:
        """Serialises mutations of one session across request threads, the
        re-engagement monitor and the verification worker."""
        return self._locks[session_id]

    def get(self, session_id: str) -> Optional[SessionState]:
        state = self.registry.get(session_id)
        if state is None:
            row = db.get_session(session_id)
            if row is not None:
                state = state_from_row(row)
                if state is not None:
                    self.registry[state.id] = state
        return state

    def find_by_contact(self, source: str, contact: str) -> Optional[SessionState]:
        row = db.find_session_by_contact(source, contact)
        if row is None:
            return None
        cached = self.registry.get(row["id"])
        if cached is not None:
            return cached
        state = state_from_row(row)
        if state is not None:
            self.registry[state.id] = state
        return state

    def put(self, state: SessionState) -> None:
        self.registry[state.id] = state

    def load_active(self) -> None:
        """Restore active sessions from SQLite so restarts don't orphan candidates."""
        for row in db.list_sessions(status="active", limit=500):
            state = state_from_row(row)
            if state is None:
                continue
            self.registry[state.id] = state
        if self.registry:
            log.info("restored %d active sessions from storage", len(self.registry))
