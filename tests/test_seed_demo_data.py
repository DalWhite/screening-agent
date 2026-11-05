"""scripts/seed_demo_data.py: a realistic demo dataset generated through the real engine.

The generator swaps the clock of the engine modules, so it runs in a subprocess
(never inside the test process) against a throwaway database.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.screening.profile import get_profile

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "seed_demo_data.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, timeout=240)


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo.db"
    result = _run("--db", str(path), "--count", "90", "--days", "30", "--seed", "7", "--reset")
    assert result.returncode == 0, result.stderr
    assert "Candidates: 90" in result.stdout
    return path


def _rows(path: Path, sql: str, *params) -> list:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchall()


def test_every_candidate_is_screened_by_the_real_engine(demo_db):
    assert _rows(demo_db, "SELECT COUNT(*) FROM sessions")[0][0] == 90
    statuses = dict(_rows(demo_db, "SELECT status, COUNT(*) FROM sessions GROUP BY status"))
    assert {"qualified", "disqualified", "abandoned", "active"} <= set(statuses)
    assert {p for (p,) in _rows(demo_db, "SELECT DISTINCT profile_id FROM sessions")} == {"construction_laborer", "delivery_courier"}
    assert len(_rows(demo_db, "SELECT DISTINCT source FROM sessions")) >= 3
    assert {lang for (lang,) in _rows(demo_db, "SELECT DISTINCT language FROM sessions")} == {"es", "en"}
    # derived data comes from the engine: knockout reasons, funnel progress, scores, conversation transcripts
    reasons = {r for (r,) in _rows(demo_db, "SELECT DISTINCT disqualify_reason FROM sessions WHERE status='disqualified'")}
    assert reasons and reasons <= {"underage", "no_work_permit", "out_of_area", "id_not_verified", "no_license"}
    for summary, turns in _rows(demo_db, "SELECT summary_json, candidate_turns FROM sessions WHERE status='qualified'"):
        assert json.loads(summary)["completed_stages"] and turns >= 5
    assert _rows(demo_db, "SELECT COUNT(*) FROM messages WHERE role='user'")[0][0] > 90 * 4
    # ID numbers never stored in clear, external checks answered
    assert _rows(demo_db, "SELECT COUNT(*) FROM messages WHERE content GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'")[0][0] == 0
    assert _rows(demo_db, "SELECT COUNT(*) FROM verifications WHERE provider='mock_id_registry' AND status='valid'")[0][0] > 0


def test_timeline_is_spread_over_the_period_and_never_in_the_future(demo_db):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for table, column in (("sessions", "started_at"), ("sessions", "last_ts"), ("messages", "ts"), ("events", "ts"),
                          ("recruiter_notes", "ts"), ("verifications", "updated_at")):
        assert _rows(demo_db, f"SELECT COUNT(*) FROM {table} WHERE {column} > ?", now)[0][0] == 0, (table, column)
    first, last = _rows(demo_db, "SELECT MIN(started_at), MAX(started_at) FROM sessions")[0]
    assert (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days >= 20
    # durations are realistic (minutes, not the milliseconds the script actually took)
    avg = _rows(demo_db, "SELECT AVG(duration_seconds) FROM sessions WHERE status='qualified'")[0][0]
    assert 60 < avg < 3600


def test_recruiters_work_the_pipeline_through_the_backoffice_service(demo_db):
    stages = {s for (s,) in _rows(demo_db, "SELECT DISTINCT pipeline_stage FROM sessions")}
    assert {"qualified", "rejected"} <= stages and stages & {"shortlisted", "interview", "offer", "hired"}
    moves = _rows(demo_db, "SELECT payload_json FROM events WHERE type='pipeline_change'")
    assert moves and all(json.loads(p)["by"] for (p,) in moves)
    assert _rows(demo_db, "SELECT COUNT(*) FROM recruiter_notes")[0][0] > 0
    assert _rows(demo_db, "SELECT COUNT(*) FROM sessions WHERE assignee IS NOT NULL")[0][0] > 0
    assert _rows(demo_db, "SELECT COUNT(*) FROM sessions WHERE pipeline_stage='interview' AND interview_at IS NULL")[0][0] == 0
    # moved by hand means the stage sticks (pipeline_manual)
    assert _rows(demo_db, "SELECT COUNT(*) FROM sessions WHERE pipeline_stage IN ('shortlisted','hired') "
                          "AND pipeline_manual=0")[0][0] == 0


def test_analytics_and_search_work_on_the_generated_data(demo_db, monkeypatch):
    from backend.analytics import metrics
    from backend.config import settings
    from backend.storage import db

    monkeypatch.setattr(settings, "db_path", str(demo_db))
    a = metrics.compute(db.search_sessions(db.SearchFilters(page_size=20000), with_facets=False)["items"])
    assert a["total_sessions"] == 90
    assert a["qualified_count"] + a["disqualified_count"] + a["abandoned_count"] + a["active_sessions"] == 90
    assert sum(a["pipeline"].values()) == 90
    assert a["disqualify_reasons"] and a["funnel"] and a["avg_duration_seconds"] > 0
    # the candidate list agrees with the chart it is opened from (analytics drill-down)
    for reason, count in a["disqualify_reasons"].items():
        assert db.search_sessions(db.SearchFilters(status=["disqualified"], disqualify_reason=[reason]))["total"] == count


def test_refuses_to_mix_with_an_existing_database(demo_db):
    result = _run("--db", str(demo_db), "--count", "5")
    assert result.returncode == 2
    assert "--reset" in result.stderr
    assert _rows(demo_db, "SELECT COUNT(*) FROM sessions")[0][0] == 90


def test_faq_entries_have_readable_labels_for_analytics():
    topics = get_profile("construction_laborer").public()["faq_topics"]
    assert topics["pay_es"] == "¿Cuánto se cobra?"
    assert all(text and len(text) <= 80 for text in topics.values())
