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


def test_legacy_sessions_are_backfilled_for_the_backoffice(tmp_path, monkeypatch):
    """Rows written by the courier-only release get a profile, pipeline stage, name and score."""
    path = tmp_path / "legacy2.db"
    data = ('{"full_name": "Laura Jiménez Ortiz", "has_license": true, "city": "Madrid", '
            '"availability": ["full_time"], "preferred_schedule": "flexible", "start_date": "2026-11-02"}')
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_SESSIONS)
        conn.execute(
            "INSERT INTO sessions (id, stage, status, qualified, started_at, last_ts, data_json) VALUES "
            "('old-q', 'qualified', 'qualified', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?)",
            (data,),
        )
        conn.execute(
            "INSERT INTO sessions (id, stage, status, started_at, last_ts, flags_json) VALUES "
            "('old-a', 'location', 'abandoned', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '[\"silence\"]')"
        )

    monkeypatch.setattr(settings, "db_path", str(path))
    db.init_db()
    db.init_db()  # idempotent

    qualified = db.get_session("old-q")
    assert qualified["profile_id"] == "delivery_courier"
    assert qualified["pipeline_stage"] == "qualified"
    assert qualified["display_name"] == "Laura Jiménez Ortiz"
    assert qualified["score"] is not None and qualified["tags"] == []
    assert db.get_session("old-a")["pipeline_stage"] == "review"
    found = db.search_sessions(db.SearchFilters(q="jimenez"))
    assert [s["id"] for s in found["items"]] == ["old-q"]
