"""The web chat channel, without HTTP: sessions by id, turns, polling, resume, early exit.

The HTTP transport (``backend/api/sessions.py``) is exercised by the integration
and e2e tests; here the channel's rules are checked directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.channels.web import DecisionIsFinal, ResumeWindowExpired, SessionNotFound, UnknownProcess, WebChannel
from backend.config import settings
from backend.storage import db

COURIER = ["sí, empecemos", "Laura Jiménez Ortiz", "sí, tengo carnet", "Madrid, Malasaña", "jornada completa",
           "mañana", "2 años en Glovo", "en 2 semanas"]


@pytest.fixture
def web(agent, store):
    return WebChannel(agent, store)


def test_a_screening_through_the_web_channel(web):
    state, opening = web.start()
    assert "Grupo Sazón" in opening and state.source == "web"
    for text in COURIER:
        result = web.message(state.id, text)
    assert result["status"] == "qualified" and result["summary"]["qualified"] is True
    messages = db.get_session(state.id)["messages"]
    assert {m["channel"] for m in messages} == {"web"}  # every line recorded on the web channel


def test_unknown_sessions_and_processes(web):
    with pytest.raises(SessionNotFound):
        web.message("nope", "hola")
    with pytest.raises(UnknownProcess):
        web.start(profile_id="no_such_process")


def test_queued_messages_are_delivered_when_the_chat_polls(web):
    state, _ = web.start()
    db.append_message(state.id, "recruiter", "¿Hablamos mañana?", state.stage, delivered=False, author="Ana")
    assert [m["content"] for m in web.pending(state.id)["messages"]] == ["¿Hablamos mañana?"]
    assert web.pending(state.id)["messages"] == []  # delivered once
    delivered = [m for m in db.get_session(state.id)["messages"] if m["role"] == "recruiter"][0]
    assert delivered["delivered"] and delivered["channel"] == "web"


def test_an_abandoned_screening_resumes_inside_the_window_only(web, agent):
    state, _ = web.start()
    web.message(state.id, COURIER[0])
    agent.end(state, reason="silence")
    result = web.message(state.id, COURIER[1])
    assert result["resumed"] and state.status == "active"

    agent.end(state, reason="silence")
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(hours=settings.resume_window_hours + 1)
    with pytest.raises(ResumeWindowExpired):
        web.message(state.id, "hola de nuevo")


def test_final_decisions_cannot_be_resumed(web):
    state, _ = web.start()
    for text in [COURIER[0], COURIER[1], "no tengo carnet"]:
        web.message(state.id, text)
    assert state.status == "disqualified"
    with pytest.raises(DecisionIsFinal):
        web.resume(state.id)


def test_leaving_early(web):
    state, _ = web.start()
    web.message(state.id, COURIER[0])
    assert web.end(state.id)["status"] == "abandoned"
    assert web.end(state.id) == {"reply": "", "status": "abandoned"}  # idempotent


def test_api_integrations_are_recorded_under_their_own_channel(web):
    """An ATS relaying its own channel through the session API is not "web"."""
    state, _ = web.start(source="ats")
    web.message(state.id, COURIER[0])
    assert {m["channel"] for m in db.get_session(state.id)["messages"]} == {"ats"}


def test_a_referral_opening_the_screening_link_switches_to_the_web(web, agent, store):
    state = agent.new_session(source="backoffice", contact="+34600111222")
    agent.opening(state)
    store.put(state)
    db.save_session(state)
    web.message(state.id, "hola")
    session = db.get_session(state.id)
    assert session["channels"] == ["backoffice", "web"]
    assert any(e["type"] == "channel_switch" and e["payload"]["to"] == "web" for e in session["events"])
