from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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
    author TEXT
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
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_notes_session ON recruiter_notes(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_contact ON sessions(source, contact);
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


def append_message(session_id: str, role: str, content: str, stage: Optional[str],
                   delivered: bool = True, author: Optional[str] = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, ts, stage, delivered, author) VALUES (?,?,?,?,?,?,?)",
            (session_id, role, content, _now(), stage, 1 if delivered else 0, author),
        )
        return int(cur.lastrowid or 0)


def pending_messages(session_id: str) -> list[dict[str, Any]]:
    """Return undelivered outbound messages (nudges, recruiter messages) and mark them delivered."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, ts, stage, author FROM messages "
            "WHERE session_id=? AND delivered=0 AND role!='user' ORDER BY id",
            (session_id,),
        ).fetchall()
        if rows:
            conn.execute(
                f"UPDATE messages SET delivered=1 WHERE id IN ({','.join('?' * len(rows))})",
                [r["id"] for r in rows],
            )
    return [dict(r) for r in rows]


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
        session["messages"] = [dict(r) for r in conn.execute(
            "SELECT id, role, content, ts, stage, delivered, author FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        )]
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
