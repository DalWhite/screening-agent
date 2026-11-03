"""Databases created before the channel work must migrate in place."""

import sqlite3

from backend.config import settings
from backend.storage import db

_LEGACY_SESSIONS = """
CREATE TABLE sessions (
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
"""


def test_init_db_adds_contact_column_to_legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_SESSIONS)
        conn.execute(
            "INSERT INTO sessions (id, stage, started_at, last_ts) VALUES ('old-1', 'name', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

    monkeypatch.setattr(settings, "db_path", str(path))
    db.init_db()  # must not raise on the pre-existing schema

    row = db.get_session("old-1")
    assert row is not None
    assert row["contact"] is None
    assert db.find_session_by_contact("whatsapp", "+34600111222") is None
