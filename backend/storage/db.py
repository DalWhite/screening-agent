from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    stage TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'es',
    source TEXT NOT NULL DEFAULT 'web',
    started_at TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    qualified INTEGER,
    outcome TEXT,
    disqualify_reason TEXT,
    dropped_at_stage TEXT,
    duration_seconds INTEGER,
    candidate_turns INTEGER NOT NULL DEFAULT 0,
    reengage_attempts INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    notes_json TEXT NOT NULL DEFAULT '[]',
    languages_json TEXT NOT NULL DEFAULT '[]',
    given_up_json TEXT NOT NULL DEFAULT '[]',
    rag_hits_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT,
    delivered INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL,
    stage TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = settings.root_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key, target in (
        ("data_json", "data"),
        ("summary_json", "summary"),
        ("flags_json", "flags"),
        ("notes_json", "notes"),
        ("languages_json", "languages"),
        ("given_up_json", "given_up"),
        ("rag_hits_json", "rag_hits"),
    ):
        out[target] = json.loads(out.pop(key) or "null")
    out["qualified"] = bool(out["qualified"]) if out["qualified"] is not None else None
    return out


def save_session(state: Any) -> None:
    summary = state.summary or {}
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (id, status, stage, language, source, started_at, last_ts,
                qualified, outcome, disqualify_reason, dropped_at_stage, duration_seconds,
                candidate_turns, reengage_attempts, data_json, summary_json, flags_json,
                notes_json, languages_json, given_up_json, rag_hits_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, stage=excluded.stage, language=excluded.language,
                last_ts=excluded.last_ts, qualified=excluded.qualified, outcome=excluded.outcome,
                disqualify_reason=excluded.disqualify_reason, dropped_at_stage=excluded.dropped_at_stage,
                duration_seconds=excluded.duration_seconds, candidate_turns=excluded.candidate_turns,
                reengage_attempts=excluded.reengage_attempts, data_json=excluded.data_json,
                summary_json=excluded.summary_json, flags_json=excluded.flags_json,
                notes_json=excluded.notes_json, languages_json=excluded.languages_json,
                given_up_json=excluded.given_up_json, rag_hits_json=excluded.rag_hits_json
            """,
            (
                state.id, state.status, state.stage.value, state.language.value, state.source,
                state.started_at.isoformat(), _now(),
                summary.get("qualified"), summary.get("outcome"), summary.get("disqualify_reason"),
                summary.get("dropped_at_stage"), summary.get("duration_seconds"),
                sum(1 for m in state.history if m.get("role") == "user"), state.reengage_attempts,
                json.dumps(state.data.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False) if summary else None,
                json.dumps(state.flags, ensure_ascii=False),
                json.dumps(state.notes, ensure_ascii=False),
                json.dumps(state.languages_seen, ensure_ascii=False),
                json.dumps(state.given_up, ensure_ascii=False),
                json.dumps(state.rag_hits, ensure_ascii=False),
            ),
        )


def append_message(session_id: str, role: str, content: str, stage: Optional[str],
                   delivered: bool = True) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, ts, stage, delivered) VALUES (?,?,?,?,?,?)",
            (session_id, role, content, _now(), stage, 1 if delivered else 0),
        )


def pending_messages(session_id: str) -> list[dict[str, Any]]:
    """Return undelivered assistant messages (re-engagement nudges) and mark them delivered."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, ts, stage FROM messages WHERE session_id=? AND delivered=0 ORDER BY id",
            (session_id,),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE messages SET delivered=1 WHERE session_id=? AND delivered=0", (session_id,)
            )
    return [dict(r) for r in rows]


def append_events(session_id: str, events: list[dict[str, Any]], stage: Optional[str]) -> None:
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO events (session_id, type, payload_json, ts, stage) VALUES (?,?,?,?,?)",
            [(session_id, e.get("type", ""), json.dumps(e.get("payload", {}), ensure_ascii=False), _now(), stage)
             for e in events],
        )


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        session = _row_to_session(row)
        session["messages"] = [dict(r) for r in conn.execute(
            "SELECT role, content, ts, stage, delivered FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        )]
        session["events"] = [
            {**dict(r), "payload": json.loads(r["payload_json"])}
            for r in conn.execute("SELECT type, payload_json, ts, stage FROM events WHERE session_id=? ORDER BY id", (session_id,))
        ]
        return session


def list_sessions(status: Optional[str] = None, qualified: Optional[bool] = None,
                  q: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions"
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status=?")
        params.append(status)
    if qualified is not None:
        where.append("qualified=?")
        params.append(1 if qualified else 0)
    if q:
        where.append("(data_json LIKE ? OR notes_json LIKE ? OR id=?)")
        like = f"%{q}%"
        params.extend([like, like, q])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_ts DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [_row_to_session(r) for r in conn.execute(sql, params)]


def active_sessions_idle(seconds: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    with get_conn() as conn:
        return [_row_to_session(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE status='active' AND last_ts < ? ORDER BY last_ts", (cutoff,)
        )]


def set_reengage_attempts(session_id: str, attempts: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET reengage_attempts=? WHERE id=?", (attempts, session_id))


def purge(retention_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE started_at < ?", (cutoff,))
        return cur.rowcount
