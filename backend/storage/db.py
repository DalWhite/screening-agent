from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Any, Optional

from ..config import settings
import re

from ..pipeline import DEFAULT_STAGES, STAGE_COLORS, SYSTEM_STAGES, auto_stage
from ..screening.language import normalize

log = logging.getLogger("sazon.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL DEFAULT 'delivery_courier',
    profile_revision INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    stage TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'es',
    source TEXT NOT NULL DEFAULT 'web',
    contact TEXT,
    started_at TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    updated_at TEXT,
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
    rag_hits_json TEXT NOT NULL DEFAULT '[]',
    state_json TEXT NOT NULL DEFAULT '{}',
    display_name TEXT,
    display_city TEXT,
    score REAL,
    search_text TEXT NOT NULL DEFAULT '',
    pipeline_stage TEXT NOT NULL DEFAULT 'screening',
    pipeline_manual INTEGER NOT NULL DEFAULT 0,
    assignee TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    interview_at TEXT,
    bot_paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT,
    delivered INTEGER NOT NULL DEFAULT 1,
    author TEXT,
    channel TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS channel_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT,
    contact TEXT,
    kind TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL,
    error_code INTEGER,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL,
    stage TEXT
);
CREATE TABLE IF NOT EXISTS recruiter_notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    latest_revision INTEGER NOT NULL DEFAULT 0,
    seed_hash TEXT,
    draft_json TEXT,
    draft_revision INTEGER NOT NULL DEFAULT 0,
    draft_updated_at TEXT,
    draft_updated_by TEXT
);
CREATE TABLE IF NOT EXISTS profile_versions (
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (profile_id, revision)
);
CREATE TABLE IF NOT EXISTS pipeline_stages (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT 'gray',
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    check_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT,
    result_detail TEXT,
    reference TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    due_at TEXT,
    deadline_at TEXT NOT NULL,
    completed_at TEXT,
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_verifications_session ON verifications(session_id);
CREATE INDEX IF NOT EXISTS idx_verifications_status ON verifications(status);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS channel_inbound (
    provider TEXT NOT NULL,
    message_id TEXT NOT NULL,
    state TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (provider, message_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_external ON channel_deliveries(provider, external_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_session ON channel_deliveries(session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_notes_session ON recruiter_notes(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_contact ON sessions(source, contact);
CREATE INDEX IF NOT EXISTS idx_sessions_contact_profile ON sessions(contact, profile_id);
CREATE INDEX IF NOT EXISTS idx_sessions_pipeline ON sessions(pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id);
"""

# Columns added after the first release: (table, column, DDL). Applied in order on old databases.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("sessions", "contact", "TEXT"),
    ("sessions", "profile_id", "TEXT NOT NULL DEFAULT 'delivery_courier'"),
    ("sessions", "updated_at", "TEXT"),
    ("sessions", "state_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("sessions", "display_name", "TEXT"),
    ("sessions", "display_city", "TEXT"),
    ("sessions", "score", "REAL"),
    ("sessions", "search_text", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "pipeline_stage", "TEXT NOT NULL DEFAULT 'screening'"),
    ("sessions", "pipeline_manual", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "assignee", "TEXT"),
    ("sessions", "tags_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("sessions", "interview_at", "TEXT"),
    ("sessions", "bot_paused", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "author", "TEXT"),
    ("sessions", "profile_revision", "INTEGER"),
    # conversation registry: which channel each message went through, what it was, details
    ("messages", "channel", "TEXT"),
    ("messages", "kind", "TEXT NOT NULL DEFAULT 'text'"),
    ("messages", "meta_json", "TEXT NOT NULL DEFAULT '{}'"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = settings.root_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; avoids an fsync per commit
    return conn


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Additive migrations for databases created by earlier releases."""
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    added: list[str] = []
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in tables:
            continue
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            added.append(f"{table}.{column}")
    return added


def init_db() -> None:
    with get_conn() as conn:
        # WAL: recruiters reading the backoffice never block candidate turns being written
        conn.execute("PRAGMA journal_mode = WAL")
        added = _migrate(conn)
        conn.executescript(_SCHEMA)
        if "sessions.pipeline_stage" in added or "sessions.display_name" in added:
            _backfill(conn)
        if not conn.execute("SELECT 1 FROM pipeline_stages LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO pipeline_stages (id, label, color, position, created_at) VALUES (?,?,?,?,?)",
                [(s["id"], s["label"], s["color"], i, _now()) for i, s in enumerate(DEFAULT_STAGES)],
            )
        if "sessions.profile_revision" in added:
            # sessions from before profile revisions were screened by the seed profiles (revision 1)
            conn.execute("UPDATE sessions SET profile_revision=1 WHERE profile_revision IS NULL")


def _backfill(conn: sqlite3.Connection) -> None:
    """Derive backoffice columns for sessions stored before they existed."""
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    for row in rows:
        session = _row_to_session(row)
        derived = _derive(session["profile_id"], session["data"] or {}, session.get("profile_revision"))
        conn.execute(
            "UPDATE sessions SET pipeline_stage=?, display_name=?, display_city=?, score=? WHERE id=?",
            (auto_stage(session["status"], session["flags"] or [], derived["review_flags"]), derived["name"], derived["city"],
             derived["score"], session["id"]),
        )
        _refresh_search_text(conn, session["id"])
    log.info("backfilled backoffice columns for %d legacy sessions", len(rows))


_JSON_COLUMNS = (
    ("data_json", "data"),
    ("summary_json", "summary"),
    ("flags_json", "flags"),
    ("notes_json", "notes"),
    ("languages_json", "languages"),
    ("given_up_json", "given_up"),
    ("rag_hits_json", "rag_hits"),
    ("state_json", "state"),
    ("tags_json", "tags"),
)


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key, target in _JSON_COLUMNS:
        if key in out:
            out[target] = json.loads(out.pop(key) or "null")
    out.pop("search_text", None)
    if "qualified" in out:
        out["qualified"] = bool(out["qualified"]) if out["qualified"] is not None else None
    for key in ("bot_paused", "pipeline_manual"):
        if key in out:
            out[key] = bool(out[key])
    state = out.get("state") or {}
    out["attempts"] = state.get("attempts") or {}
    out["abuse_count"] = state.get("abuse_count") or 0
    out["completed_stages"] = state.get("completed_stages") or []
    if "source" in out:  # sessions saved before channel continuity only know where they started
        out["channels"] = state.get("channels") or [out["source"]]
        out["channel"] = state.get("channel") or out["channels"][-1]
        channel_state = dict(state.get("channel_state") or {})
        if state.get("wa_inbound_at") and "whatsapp" not in channel_state:  # saved before channel_state existed
            channel_state["whatsapp"] = {"inbound_at": state["wa_inbound_at"]}
        out["channel_state"] = channel_state
    out["profile_id"] = out.get("profile_id") or "delivery_courier"
    return out


def _derive(profile_id: Optional[str], data: dict[str, Any], revision: Optional[int] = None) -> dict[str, Any]:
    """Profile-aware denormalised columns: display name/city and ranking score."""
    name = data.get("full_name")
    city = data.get("city")
    score = None
    review_flags: set[str] = set()
    try:
        from ..screening.profile import get_profile

        profile = get_profile(profile_id, revision)
        name_field = profile.field_of_type("name")
        city_field = profile.field_of_type("location")
        name = data.get(name_field) if name_field else name
        city = data.get(city_field) if city_field else city
        score, _ = profile.score(data)
        review_flags = profile.review_flags()
    except (KeyError, ValueError):
        pass
    return {"name": name, "city": city, "score": score, "review_flags": review_flags}


def _refresh_search_text(conn: sqlite3.Connection, session_id: str) -> None:
    """Accent/case-insensitive haystack for backoffice search (no transcript: that is searched live)."""
    row = conn.execute(
        "SELECT id, contact, display_name, display_city, data_json, notes_json, flags_json, tags_json, assignee "
        "FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if row is None:
        return
    parts = [row["id"], row["contact"] or "", row["display_name"] or "", row["display_city"] or "",
             row["assignee"] or ""]
    for key in ("data_json", "notes_json", "flags_json", "tags_json"):
        try:
            value = json.loads(row[key] or "null")
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values() if v not in (None, "", []))
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)
    notes = conn.execute("SELECT body FROM recruiter_notes WHERE session_id=?", (session_id,)).fetchall()
    parts.extend(n["body"] for n in notes)
    text = " ".join(normalize(p) for p in parts if p)
    conn.execute("UPDATE sessions SET search_text=? WHERE id=?", (f" {text} ", session_id))


def save_session(state: Any) -> None:
    summary = state.summary or {}
    data = dict(state.data)
    derived = _derive(getattr(state, "profile_id", None), data, getattr(state, "profile_revision", None))
    flags = list(state.flags)
    bot_paused = bool(getattr(state, "bot_paused", False))
    new_auto = auto_stage(state.status, flags, derived["review_flags"])
    agent_state = {
        "attempts": dict(getattr(state, "attempts", {}) or {}),
        "abuse_count": getattr(state, "abuse_count", 0),
        "completed_stages": list(getattr(state, "completed_stages", []) or []),
        "channel": getattr(state, "channel", None),
        "channels": list(getattr(state, "channels", []) or []),
        "channel_state": dict(getattr(state, "channel_state", {}) or {}),
    }
    disqualify_reason = summary.get("disqualify_reason") or getattr(state, "disqualify_reason", None)
    with get_conn() as conn:
        before = conn.execute("SELECT pipeline_stage, pipeline_manual FROM sessions WHERE id=?", (state.id,)).fetchone()
        conn.execute(
            """
            INSERT INTO sessions (id, profile_id, profile_revision, status, stage, language, source, contact, started_at,
                last_ts, updated_at, qualified, outcome, disqualify_reason, dropped_at_stage, duration_seconds,
                candidate_turns, reengage_attempts, data_json, summary_json, flags_json,
                notes_json, languages_json, given_up_json, rag_hits_json, state_json,
                display_name, display_city, score, pipeline_stage, bot_paused)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                profile_id=excluded.profile_id, profile_revision=excluded.profile_revision,
                status=excluded.status, stage=excluded.stage, language=excluded.language,
                contact=excluded.contact,
                last_ts=excluded.last_ts, updated_at=excluded.updated_at,
                qualified=excluded.qualified, outcome=excluded.outcome,
                disqualify_reason=excluded.disqualify_reason, dropped_at_stage=excluded.dropped_at_stage,
                duration_seconds=excluded.duration_seconds, candidate_turns=excluded.candidate_turns,
                reengage_attempts=excluded.reengage_attempts, data_json=excluded.data_json,
                summary_json=excluded.summary_json, flags_json=excluded.flags_json,
                notes_json=excluded.notes_json, languages_json=excluded.languages_json,
                given_up_json=excluded.given_up_json, rag_hits_json=excluded.rag_hits_json,
                state_json=excluded.state_json,
                display_name=excluded.display_name, display_city=excluded.display_city, score=excluded.score,
                bot_paused=excluded.bot_paused,
                pipeline_stage=CASE WHEN sessions.pipeline_manual=1 THEN sessions.pipeline_stage
                                    ELSE excluded.pipeline_stage END
            """,
            (
                state.id, getattr(state, "profile_id", None) or "delivery_courier",
                getattr(state, "profile_revision", None), state.status, state.stage,
                state.language.value, state.source, getattr(state, "contact", None),
                state.started_at.isoformat(), state.last_candidate_ts.isoformat(timespec="seconds"), _now(),
                summary.get("qualified"), summary.get("outcome"), disqualify_reason,
                summary.get("dropped_at_stage"), summary.get("duration_seconds"),
                sum(1 for m in state.history if m.get("role") == "user"), state.reengage_attempts,
                json.dumps(data, ensure_ascii=False, default=str),
                json.dumps(summary, ensure_ascii=False, default=str) if summary else None,
                json.dumps(flags, ensure_ascii=False),
                json.dumps(state.notes, ensure_ascii=False),
                json.dumps(state.languages_seen, ensure_ascii=False),
                json.dumps(state.given_up, ensure_ascii=False),
                json.dumps(state.rag_hits, ensure_ascii=False),
                json.dumps(agent_state, ensure_ascii=False),
                derived["name"], derived["city"], derived["score"], new_auto, 1 if bot_paused else 0,
            ),
        )
        if before is not None and not before["pipeline_manual"] and before["pipeline_stage"] != new_auto:
            conn.execute(
                "INSERT INTO events (session_id, type, payload_json, ts, stage) VALUES (?,?,?,?,?)",
                (state.id, "pipeline_auto", json.dumps({"from": before["pipeline_stage"], "to": new_auto}),
                 _now(), state.stage),
            )
        _refresh_search_text(conn, state.id)


# Message kinds in the conversation registry. Candidate side: what they sent; assistant side:
# what kind of prompt it was. Everything a candidate reads or hears is stored, on every channel.
MESSAGE_KINDS = (
    "text",        # typed message (web, WhatsApp) or a regular assistant reply
    "speech",      # what a caller said, as transcribed by the phone provider's STT
    "voice_note",  # WhatsApp voice note, stored as its transcript (the audio is never kept)
    "media",       # WhatsApp photo / video / document / sticker: a placeholder, never the file
    "reprompt",    # assistant re-asked after silence on a call
    "notice",      # assistant system notice (recruiter is reviewing, media not supported...)
    "nudge",       # re-engagement message to a silent candidate
)


def append_message(session_id: str, role: str, content: str, stage: Optional[str],
                   delivered: bool = True, author: Optional[str] = None, channel: Optional[str] = None,
                   kind: str = "text", meta: Optional[dict[str, Any]] = None) -> int:
    """Store one conversation line. ``channel`` is where it was said or delivered (None = not
    delivered yet); ``meta`` keeps channel details (STT confidence, call SID, provider ids...)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, ts, stage, delivered, author, channel, kind, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, role, content, _now(), stage, 1 if delivered else 0, author,
             channel if delivered or role == "user" else None, kind,
             json.dumps(meta or {}, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid or 0)


IN_FLIGHT = 2  # messages.delivered while a push is on its way (not picked up by pending_messages)
SUPERSEDED = 3  # never sent and no longer relevant (a nudge for a candidate who already came back)


def push_message(session_id: str, role: str, content: str, stage: Optional[str], push: Callable[[], Any],
                 author: Optional[str] = None, kind: str = "text") -> bool:
    """Outbox: store an outbound message *before* pushing it, then settle it.

    Providers can report a failure (status callback) before the push call has
    even returned; storing first means that report always finds the row. The
    row is settled as delivered only if nothing re-queued it in the meantime.
    ``push`` returns the name of the channel that delivered it (recorded with the
    message) or a falsy value.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, ts, stage, delivered, author, channel, kind, meta_json) "
            "VALUES (?,?,?,?,?,?,?,NULL,?,'{}')",
            (session_id, role, content, _now(), stage, IN_FLIGHT, author, kind),
        )
        message_id = int(cur.lastrowid or 0)
    try:
        result = push()  # the channel that delivered it ("whatsapp"...), or a falsy value
    except Exception:  # a failed push must not lose the message: it stays queued
        log.exception("push failed for session %s", session_id)
        result = None
    delivered = bool(result)
    channel = result if isinstance(result, str) else None
    with get_conn() as conn:
        if delivered:
            conn.execute("UPDATE messages SET delivered=1, channel=? WHERE id=? AND delivered=?",
                         (channel, message_id, IN_FLIGHT))
        else:
            conn.execute("UPDATE messages SET delivered=0 WHERE id=? AND delivered=?", (message_id, IN_FLIGHT))
    return delivered


def pending_messages(session_id: str, channel: Optional[str] = None,
                     supersede: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Return undelivered outbound messages (nudges, recruiter messages) and mark them delivered
    (through ``channel``, when the caller knows it: "whatsapp", "web"...). Kinds in ``supersede``
    are not returned but marked superseded: e.g. "are you still there?" for a candidate who is."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, ts, stage, author, kind FROM messages "
            "WHERE session_id=? AND delivered=0 AND role!='user' ORDER BY id",
            (session_id,),
        ).fetchall()
        stale = [r["id"] for r in rows if r["kind"] in supersede]
        if stale:
            conn.execute(f"UPDATE messages SET delivered=? WHERE id IN ({','.join('?' * len(stale))})",
                         [SUPERSEDED, *stale])
        rows = [r for r in rows if r["kind"] not in supersede]
        if rows:
            conn.execute(
                f"UPDATE messages SET delivered=1, channel=COALESCE(?, channel) WHERE id IN ({','.join('?' * len(rows))})",
                [channel, *[r["id"] for r in rows]],
            )
    return [dict(r) for r in rows]


def requeue_message(session_id: str, content: str) -> Optional[int]:
    """A push the provider accepted and later failed: mark the latest outbound copy undelivered
    again, so it goes out with the candidate's next message."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE session_id=? AND role!='user' AND content=? ORDER BY id DESC LIMIT 1",
            (session_id, content),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE messages SET delivered=0, channel=NULL WHERE id=?", (row["id"],))
        return int(row["id"])


# ---------- provider delivery registry (outbound WhatsApp messages) ----------

# ---------- inbound de-duplication (provider webhook retries, across restarts) ----------

INBOUND_STALE_SECONDS = 60  # a claim still "processing" after this long died with its process


def claim_inbound(provider: str, message_id: str) -> bool:
    """True if this provider message must be processed now; False for a retry of one already
    processed (or being processed). A claim left "processing" by a crashed process is taken over,
    so a redelivery after a crash mid-turn is not lost."""
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        row = conn.execute("SELECT state, received_at FROM channel_inbound WHERE provider=? AND message_id=?",
                           (provider, message_id)).fetchone()
        if row is not None:
            stale = now - datetime.fromisoformat(row["received_at"]) > timedelta(seconds=INBOUND_STALE_SECONDS)
            if row["state"] == "done" or not stale:
                return False
        conn.execute("INSERT OR REPLACE INTO channel_inbound (provider, message_id, state, received_at) VALUES (?,?,?,?)",
                     (provider, message_id, "processing", now.isoformat()))
        if now.minute == 0 and now.second < 2:  # cheap housekeeping: providers retry for days, not weeks
            conn.execute("DELETE FROM channel_inbound WHERE received_at < ?", ((now - timedelta(days=8)).isoformat(),))
    return True


def finish_inbound(provider: str, message_id: str, processed: bool = True) -> None:
    with get_conn() as conn:
        if processed:
            conn.execute("UPDATE channel_inbound SET state='done' WHERE provider=? AND message_id=?", (provider, message_id))
        else:  # failed: let the provider's retry through
            conn.execute("DELETE FROM channel_inbound WHERE provider=? AND message_id=?", (provider, message_id))


def session_for_event(event_type: str, key: str, value: str) -> Optional[str]:
    """Session of the latest event whose payload has ``key == value`` (e.g. the call of a CallSid)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM events WHERE type=? AND json_extract(payload_json, ?) = ? ORDER BY id DESC LIMIT 1",
            (event_type, f"$.{key}", value)).fetchone()
        return row["session_id"] if row else None


# kind: reply (answer to the candidate) | message (recruiter / check decision) | nudge | template
def record_delivery(session_id: Optional[str], channel: str, provider: str, external_id: Optional[str],
                    contact: Optional[str], kind: str, body: Optional[str], status: str,
                    error_code: Optional[int] = None, error_detail: Optional[str] = None) -> int:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO channel_deliveries (session_id, channel, provider, external_id, contact, kind, body, status, "
            "error_code, error_detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, channel, provider, external_id, contact, kind, body, status, error_code, error_detail, now, now),
        )
        return int(cur.lastrowid or 0)


_DELIVERY_RANK = {"accepted": 0, "queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4, "undelivered": 4}


def update_delivery(provider: str, external_id: str, status: str, error_code: Optional[int] = None,
                    error_detail: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Apply a provider status callback. Statuses only move forward (a late "sent" never
    overwrites "read" or "failed"), atomically: receipts can be processed concurrently.
    Returns the delivery row as it was before, plus ``applied`` (this call changed it),
    or None if the message id is unknown."""
    rank = " ".join(f"WHEN '{name}' THEN {value}" for name, value in _DELIVERY_RANK.items())
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM channel_deliveries WHERE provider=? AND external_id=? ORDER BY id DESC LIMIT 1",
                           (provider, external_id)).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE channel_deliveries SET status=?, error_code=COALESCE(?, error_code), "
            "error_detail=COALESCE(?, error_detail), updated_at=? "
            f"WHERE id=? AND status != ? AND (CASE status {rank} ELSE 0 END) <= ?",
            (status, error_code, error_detail, _now(), row["id"], status, _DELIVERY_RANK.get(status, 0)))
        return {**dict(row), "applied": cur.rowcount == 1}


def deliveries(session_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM channel_deliveries WHERE session_id=? ORDER BY id", (session_id,))]


def append_events(session_id: str, events: list[dict[str, Any]], stage: Optional[str]) -> None:
    if not events:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO events (session_id, type, payload_json, ts, stage) VALUES (?,?,?,?,?)",
            [(session_id, e.get("type", ""), json.dumps(e.get("payload", {}), ensure_ascii=False, default=str), _now(), stage)
             for e in events],
        )


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        session = _row_to_session(row)
        session["messages"] = [
            {**{k: r[k] for k in ("id", "role", "content", "ts", "stage", "delivered", "author", "channel", "kind")},
             "meta": json.loads(r["meta_json"] or "{}")}
            for r in conn.execute(
                "SELECT id, role, content, ts, stage, delivered, author, channel, kind, meta_json FROM messages "
                "WHERE session_id=? ORDER BY id", (session_id,))
        ]
        session["events"] = [
            {**{k: r[k] for k in ("type", "ts", "stage")}, "payload": json.loads(r["payload_json"])}
            for r in conn.execute("SELECT type, payload_json, ts, stage FROM events WHERE session_id=? ORDER BY id", (session_id,))
        ]
        return session


def list_sessions(status: Optional[str] = None, qualified: Optional[bool] = None,
                  q: Optional[str] = None, limit: int = 200, profile_id: Optional[str] = None) -> list[dict[str, Any]]:
    filters = SearchFilters(
        q=q, status=[status] if status else [], qualified=qualified,
        profile_id=[profile_id] if profile_id else [], page_size=limit,
    )
    return search_sessions(filters, with_facets=False)["items"]


def find_session_by_contact(source: str, contact: str) -> Optional[dict[str, Any]]:
    """Latest session for a channel identity (e.g. a WhatsApp number), newest first."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE source=? AND contact=? ORDER BY last_ts DESC, started_at DESC LIMIT 1",
            (source, contact),
        ).fetchone()
        return _row_to_session(row) if row is not None else None


LIVE_STATUSES = ("active", "verifying", "abandoned")


def find_live_sessions_by_contact(contact: str, profile_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Not-yet-decided sessions of one phone number in one process, on any channel, newest first.

    Channel continuity: a candidate who started on WhatsApp can carry on by phone
    (and vice versa), or continue a screening a recruiter registered for them.
    Callers still apply the lifecycle rules (resume window, final stages).
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM sessions WHERE contact=? AND profile_id=? AND status IN ({','.join('?' * len(LIVE_STATUSES))}) "
            "ORDER BY last_ts DESC, started_at DESC LIMIT ?",
            (contact, profile_id, *LIVE_STATUSES, limit),
        ).fetchall()
        return [_row_to_session(r) for r in rows]


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


# ---------- backoffice: recruiter-owned columns ----------


# ---------- backoffice: configurable pipeline stages ----------


class StageError(ValueError):
    pass


def list_pipeline_stages() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.label, s.color, s.position, "
            "(SELECT COUNT(*) FROM sessions WHERE pipeline_stage = s.id) AS candidates "
            "FROM pipeline_stages s ORDER BY s.position, s.created_at"
        ).fetchall()
    return [{**dict(r), "system": r["id"] in SYSTEM_STAGES} for r in rows]


def pipeline_ids() -> list[str]:
    with get_conn() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM pipeline_stages ORDER BY position, created_at")]


def _clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", str(label or "")).strip()[:40]
    if not label:
        raise StageError("el nombre de la etapa no puede estar vacío")
    return label


def create_pipeline_stage(label: str, color: str = "indigo", after: Optional[str] = None) -> dict[str, Any]:
    label = _clean_label(label)
    if color not in STAGE_COLORS:
        raise StageError(f"color no válido: {color}")
    base = re.sub(r"[^a-z0-9]+", "_", normalize(label)).strip("_")[:24] or "etapa"
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM pipeline_stages ORDER BY position, created_at")]
        if any(r["label"].lower() == label.lower() for r in conn.execute("SELECT label FROM pipeline_stages")):
            raise StageError(f"ya existe una etapa «{label}»")
        stage_id, n = base, 2
        while stage_id in ids:
            stage_id, n = f"{base}_{n}", n + 1
        if after in ids:
            ids.insert(ids.index(after) + 1, stage_id)
        elif "rejected" in ids:  # new stages go before the final "rejected" column by default
            ids.insert(ids.index("rejected"), stage_id)
        else:
            ids.append(stage_id)
        conn.execute("INSERT INTO pipeline_stages (id, label, color, position, created_at) VALUES (?,?,?,?,?)",
                     (stage_id, label, color, 0, _now()))
        conn.executemany("UPDATE pipeline_stages SET position=? WHERE id=?", [(i, sid) for i, sid in enumerate(ids)])
    return {"id": stage_id, "label": label, "color": color}


def update_pipeline_stage(stage_id: str, label: Optional[str] = None, color: Optional[str] = None) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pipeline_stages WHERE id=?", (stage_id,)).fetchone()
        if row is None:
            raise KeyError(stage_id)
        new_label = _clean_label(label) if label is not None else row["label"]
        new_color = color if color is not None else row["color"]
        if new_color not in STAGE_COLORS:
            raise StageError(f"color no válido: {new_color}")
        clash = conn.execute("SELECT 1 FROM pipeline_stages WHERE lower(label)=lower(?) AND id!=?", (new_label, stage_id)).fetchone()
        if clash:
            raise StageError(f"ya existe una etapa «{new_label}»")
        conn.execute("UPDATE pipeline_stages SET label=?, color=? WHERE id=?", (new_label, new_color, stage_id))
    return {"id": stage_id, "label": new_label, "color": new_color}


def reorder_pipeline_stages(ordered_ids: list[str]) -> None:
    with get_conn() as conn:
        current = [r["id"] for r in conn.execute("SELECT id FROM pipeline_stages")]
        if sorted(current) != sorted(ordered_ids):
            raise StageError("la lista de etapas no coincide con las existentes")
        conn.executemany("UPDATE pipeline_stages SET position=? WHERE id=?", [(i, sid) for i, sid in enumerate(ordered_ids)])


def delete_pipeline_stage(stage_id: str, move_to: Optional[str] = None) -> list[str]:
    """Delete a custom stage; its candidates move to ``move_to``. Returns the moved session ids."""
    if stage_id in SYSTEM_STAGES:
        raise StageError("las etapas del sistema no se pueden eliminar (sí renombrar)")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM pipeline_stages WHERE id=?", (stage_id,)).fetchone() is None:
            raise KeyError(stage_id)
        moved = [r["id"] for r in conn.execute("SELECT id FROM sessions WHERE pipeline_stage=?", (stage_id,))]
        if moved:
            if not move_to or move_to == stage_id or conn.execute(
                    "SELECT 1 FROM pipeline_stages WHERE id=?", (move_to,)).fetchone() is None:
                raise StageError(f"la etapa tiene {len(moved)} candidato(s): elige a qué etapa moverlos")
            conn.execute("UPDATE sessions SET pipeline_stage=?, pipeline_manual=1, updated_at=? WHERE pipeline_stage=?",
                         (move_to, _now(), stage_id))
        conn.execute("DELETE FROM pipeline_stages WHERE id=?", (stage_id,))
    return moved


def set_pipeline_stage(session_id: str, stage: str) -> Optional[str]:
    """Recruiter move. Returns the previous stage (None if the session does not exist)."""
    if stage not in pipeline_ids():
        raise ValueError(f"unknown pipeline stage {stage!r}")
    with get_conn() as conn:
        row = conn.execute("SELECT pipeline_stage FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE sessions SET pipeline_stage=?, pipeline_manual=1, updated_at=? WHERE id=?",
            (stage, _now(), session_id),
        )
        return row["pipeline_stage"]


def update_recruiter_fields(session_id: str, **values: Any) -> None:
    """Update assignee / tags / interview_at (recruiter-owned, never touched by the agent)."""
    allowed = {"assignee": "assignee", "tags": "tags_json", "interview_at": "interview_at"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(f"field {key!r} is not editable")
        sets.append(f"{allowed[key]}=?")
        params.append(json.dumps(value, ensure_ascii=False) if key == "tags" else value)
    if not sets:
        return
    with get_conn() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)}, updated_at=? WHERE id=?", (*params, _now(), session_id))
        _refresh_search_text(conn, session_id)


def add_note(session_id: str, author: str, body: str) -> dict[str, Any]:
    note = {"id": uuid.uuid4().hex[:12], "session_id": session_id, "author": author, "body": body, "ts": _now()}
    with get_conn() as conn:
        conn.execute("INSERT INTO recruiter_notes (id, session_id, author, body, ts) VALUES (?,?,?,?,?)",
                     (note["id"], session_id, author, body, note["ts"]))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (_now(), session_id))
        _refresh_search_text(conn, session_id)
    return note


def list_notes(session_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, session_id, author, body, ts FROM recruiter_notes WHERE session_id=? ORDER BY ts DESC, rowid DESC",
            (session_id,),
        )]


def delete_note(session_id: str, note_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM recruiter_notes WHERE id=? AND session_id=?", (note_id, session_id))
        _refresh_search_text(conn, session_id)
        return cur.rowcount > 0


def all_tags() -> list[str]:
    with get_conn() as conn:
        tags: set[str] = set()
        for row in conn.execute("SELECT tags_json FROM sessions WHERE tags_json != '[]'"):
            tags.update(json.loads(row["tags_json"] or "[]"))
    return sorted(tags)


def all_assignees() -> list[str]:
    with get_conn() as conn:
        return [r["assignee"] for r in conn.execute(
            "SELECT DISTINCT assignee FROM sessions WHERE assignee IS NOT NULL AND assignee != '' ORDER BY assignee"
        )]


# ---------- backoffice: search ----------

SORTABLE = {
    "last_ts": "last_ts",
    "started_at": "started_at",
    "score": "score",
    "name": "display_name COLLATE NOCASE",
    "city": "display_city COLLATE NOCASE",
    "updated_at": "updated_at",
}


@dataclass
class SearchFilters:
    q: Optional[str] = None
    profile_id: list[str] = field(default_factory=list)
    pipeline_stage: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    language: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    assignee: Optional[str] = None           # "__none__" = unassigned
    qualified: Optional[bool] = None
    bot_paused: Optional[bool] = None
    min_score: Optional[float] = None
    max_score: Optional[float] = None
    started_from: Optional[str] = None       # ISO date, inclusive
    started_to: Optional[str] = None         # ISO date, inclusive
    city: Optional[str] = None
    disqualify_reason: list[str] = field(default_factory=list)
    dropped_at: list[str] = field(default_factory=list)   # abandoned at this question
    reached: Optional[str] = None                          # completed this stage of the funnel
    faq: Optional[str] = None                              # asked this FAQ item
    sort: str = "last_ts"
    order: str = "desc"
    page: int = 1
    page_size: int = 25


def _where(f: SearchFilters, skip: tuple[str, ...] = ()) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    def any_of(column: str, values: list[str]) -> None:
        values = [v for v in values if v]
        if values:
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)

    if f.q:
        for token in normalize(f.q).split()[:6]:
            like = f"%{token}%"
            clauses.append(
                "(search_text LIKE ? OR id LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id "
                "AND m.role = 'user' AND m.content LIKE ?))"
            )
            params.extend([like, f"{token}%", like])
    for column in ("profile_id", "pipeline_stage", "status", "source", "language", "disqualify_reason"):
        if column not in skip:
            any_of(column, getattr(f, column))
    any_of("dropped_at_stage", f.dropped_at)
    if f.reached:
        if f.reached == "qualified":
            clauses.append("status = 'qualified'")
        else:
            clauses.append("(EXISTS (SELECT 1 FROM json_each(sessions.state_json, '$.completed_stages') WHERE value = ?) "
                           "OR EXISTS (SELECT 1 FROM json_each(sessions.summary_json, '$.completed_stages') WHERE value = ?))")
            params.extend([f.reached, f.reached])
    if f.faq:
        clauses.append("rag_hits_json LIKE ?")
        params.append(f'%{json.dumps(f.faq)}%')
    if f.flags:
        ors = []
        for flag in f.flags:
            ors.append("flags_json LIKE ?")
            params.append(f'%"{flag}%')
        clauses.append("(" + " OR ".join(ors) + ")")
    for tag in f.tags:
        clauses.append("tags_json LIKE ?")
        params.append(f'%{json.dumps(tag, ensure_ascii=False)}%')
    if f.assignee == "__none__":
        clauses.append("(assignee IS NULL OR assignee = '')")
    elif f.assignee:
        clauses.append("assignee = ?")
        params.append(f.assignee)
    if f.qualified is not None:
        clauses.append("qualified = ?" if f.qualified else "(qualified IS NULL OR qualified = 0)")
        if f.qualified:
            params.append(1)
    if f.bot_paused is not None:
        clauses.append("bot_paused = ?")
        params.append(1 if f.bot_paused else 0)
    if f.min_score is not None:
        clauses.append("score >= ?")
        params.append(f.min_score)
    if f.max_score is not None:
        clauses.append("score <= ?")
        params.append(f.max_score)
    if f.started_from:
        clauses.append("started_at >= ?")
        params.append(f.started_from[:10])
    if f.started_to:
        clauses.append("started_at < ?")
        params.append((datetime.fromisoformat(f.started_to[:10]) + timedelta(days=1)).date().isoformat())
    if f.city:
        clauses.append("search_text LIKE ?")
        params.append(f"%{normalize(f.city)}%")
        clauses.append("display_city IS NOT NULL")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search_sessions(f: SearchFilters, with_facets: bool = True) -> dict[str, Any]:
    where, params = _where(f)
    column = SORTABLE.get(f.sort, "last_ts")
    order = "ASC" if str(f.order).lower() == "asc" else "DESC"
    nulls = f"CASE WHEN {column.split()[0]} IS NULL THEN 1 ELSE 0 END"
    page_size = max(1, min(int(f.page_size or 25), 20000))
    page = max(1, int(f.page or 1))
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM sessions{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM sessions{where} ORDER BY {nulls}, {column} {order}, last_ts DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        result: dict[str, Any] = {
            "items": [_row_to_session(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
        if with_facets:
            facets: dict[str, dict[str, int]] = {}
            for facet in ("pipeline_stage", "status", "profile_id", "source"):
                fw, fp = _where(f, skip=(facet,))
                facets[facet] = {
                    r[0]: r[1] for r in conn.execute(f"SELECT {facet}, COUNT(*) FROM sessions{fw} GROUP BY {facet}", fp)
                }
            result["facets"] = facets
    return result
