"""Identity analysis §5: one candidate, several things happening at once, one or several workers.

A "worker" is modelled as what a second uvicorn process has of its own: a ``SessionStore`` (the
in-memory registry and per-session locks) and channel instances (per-number locks, seen message
ids), over the same database.

Already covered (and passing), not repeated here: two messages of one number on one worker are
serialised (``tests/unit/channels/test_messaging_channel.py::test_turns_for_same_contact_are_serialised``),
and the same candidate on WhatsApp and on the phone at once *once the screening exists*
(``tests/e2e/channels/test_resilience.py::test_the_same_candidate_on_whatsapp_and_on_the_phone_at_once``).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from backend.backoffice.service import BackofficeService
from backend.channels.messaging import MessagingChannel
from backend.config import settings
from backend.reengagement import ReengagementMonitor
from backend.session_store import SessionStore
from backend.storage import db
from tests.support.identity import (DEFERRED_MONITOR, DEFERRED_MULTI_WORKER, at_once, courier, send_whatsapp,
                                    sessions_for, with_db_latency)

ROUNDS = 10
LAURA = "+34600111222"


# ---------- first contact on two paths at once ----------

def test_first_contact_on_whatsapp_and_on_the_phone_at_once_opens_one_screening(whatsapp, voice, monkeypatch):
    with_db_latency(monkeypatch)
    doubles = []
    for i in range(ROUNDS):
        number = f"+3460090{i:04d}"
        at_once(lambda: send_whatsapp(whatsapp, number, "hola"), lambda: voice.start_call(number, f"CA-both{i}"))
        if len(sessions_for(number)) != 1:
            doubles.append(number)
    assert not doubles, f"{len(doubles)}/{ROUNDS} candidates got two screenings"


@DEFERRED_MULTI_WORKER
def test_first_contact_on_two_workers_opens_one_screening(agent, provider, temp_db, monkeypatch):
    """Two different messages of a new candidate, each handled by another worker."""
    with_db_latency(monkeypatch)
    doubles = []
    for i in range(ROUNDS):
        number = f"+3460091{i:04d}"
        first, second = (MessagingChannel(agent, SessionStore(), provider, profile_id="delivery_courier")
                         for _ in range(2))
        at_once(lambda: send_whatsapp(first, number, "hola"), lambda: send_whatsapp(second, number, "buenas"))
        if len(sessions_for(number)) != 1:
            doubles.append(number)
    assert not doubles, f"{len(doubles)}/{ROUNDS} candidates got two screenings"


# ---------- state cached by another worker ----------

def _two_workers(agent, provider):
    """Worker A answers the candidate; worker B serves the recruiter (who opened the file early on)."""
    store_a, store_b = SessionStore(), SessionStore()
    line_a = MessagingChannel(agent, store_a, provider, profile_id="delivery_courier")
    backoffice_b = BackofficeService(agent, store_b, notifier=lambda state, text: None)
    answers = courier()
    send_whatsapp(line_a, LAURA, "hola", *answers[:2])  # name given
    [row] = sessions_for(LAURA)
    backoffice_b.detail(row["id"])
    store_b.get(row["id"])  # worker B now holds its copy
    send_whatsapp(line_a, LAURA, *answers[2:4])  # licence and city answered on worker A
    return row["id"], line_a, backoffice_b


@DEFERRED_MULTI_WORKER
def test_a_recruiter_action_on_another_worker_does_not_roll_back_the_candidates_answers(agent, provider, temp_db):
    session_id, _, backoffice_b = _two_workers(agent, provider)
    before = db.get_session(session_id)
    assert before["stage"] == "availability" and before["data"]["city"] == "Madrid"

    backoffice_b.takeover(session_id, True, by="Ana")

    after = db.get_session(session_id)
    assert after["stage"] == "availability", f"stage rolled back to {after['stage']!r}"
    assert after["data"]["city"] == "Madrid" and after["data"]["has_license"] is True, after["data"]


@DEFERRED_MULTI_WORKER
def test_a_takeover_on_another_worker_silences_the_assistant(agent, provider, temp_db):
    session_id, line_a, backoffice_b = _two_workers(agent, provider)

    backoffice_b.takeover(session_id, True, by="Ana")
    replies = send_whatsapp(line_a, LAURA, "¿hola? ¿seguís ahí?")

    assert replies == [], f"the assistant kept answering during a takeover: {replies!r}"


# ---------- background jobs ----------

@DEFERRED_MONITOR
def test_the_monitor_does_not_close_a_screening_while_the_candidate_is_answering(whatsapp, store, agent, monkeypatch):
    send_whatsapp(whatsapp, LAURA, "hola", *courier()[:2])
    state = store.find_by_contact("whatsapp", LAURA)
    state.reengage_attempts = settings.reengage_max_attempts  # nudged twice, about to be closed...
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(seconds=settings.session_idle_seconds + 5)
    process = agent.process
    answering = threading.Event()

    def slow_turn(state, text):  # the model takes its time: the candidate's answer is in flight
        answering.set()
        time.sleep(0.3)
        return process(state, text)

    monkeypatch.setattr(agent, "process", slow_turn)
    turn = threading.Thread(target=lambda: send_whatsapp(whatsapp, LAURA, "sí, tengo carnet"))
    turn.start()
    answering.wait(2)
    ReengagementMonitor(agent, store.registry).tick()  # ... when the monitor ticks
    turn.join()

    session = db.get_session(state.id)
    assert session["status"] == "active" and session["stage"] == "location", (
        f"closed while answering: {session['status']} at {session['stage']}")


@DEFERRED_MULTI_WORKER
def test_two_workers_do_not_nudge_the_same_candidate_twice(whatsapp, agent, temp_db):
    send_whatsapp(whatsapp, LAURA, "hola", *courier()[:2])
    [row] = sessions_for(LAURA)
    silent_since = (datetime.now(timezone.utc) - timedelta(seconds=settings.reengage_first_delay + 5))
    with db.get_conn() as conn:
        conn.execute("UPDATE sessions SET last_ts=? WHERE id=?", (silent_since.isoformat(timespec="seconds"), row["id"]))
    worker_a, worker_b = SessionStore(), SessionStore()
    worker_a.load_active()
    worker_b.load_active()

    ReengagementMonitor(agent, worker_a.registry).tick()
    ReengagementMonitor(agent, worker_b.registry).tick()

    nudges = [m for m in db.get_session(row["id"])["messages"] if m["kind"] == "nudge"]
    assert len(nudges) == 1, f"{len(nudges)} nudges for one silence"


@DEFERRED_MONITOR
def test_after_a_restart_every_idle_screening_is_looked_after(agent, temp_db):
    """Only 500 active sessions are reloaded at start-up: the rest are never nudged nor closed."""
    idle_since = datetime.now(timezone.utc) - timedelta(seconds=settings.session_idle_seconds + 60)
    ids = []
    for i in range(501):
        state = agent.new_session(source="whatsapp", contact=f"+3460092{i:04d}")
        state.history.append({"role": "user", "content": "hola"})
        state.reengage_attempts = settings.reengage_max_attempts
        state.last_candidate_ts = idle_since
        db.save_session(state)
        db.append_message(state.id, "user", "hola", state.stage)
        ids.append(state.id)

    restarted = SessionStore()
    restarted.load_active()
    ReengagementMonitor(agent, restarted.registry).tick()

    with db.get_conn() as conn:
        still_open = conn.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0]
    assert still_open == 0, f"{still_open} idle screenings are never closed after a restart"
