"""Identity analysis §2B: the candidate comes back later, or applies to another process.

Already covered (and passing), not repeated here:

* an abandoned screening resumes inside the 48h window (WhatsApp, phone, web):
  ``tests/unit/channels/test_messaging_channel.py::test_abandoned_session_resumes_within_window``,
  ``tests/unit/integrations/test_twilio_voice.py::test_calling_back_resumes_abandoned_session``,
  ``tests/unit/channels/test_web_channel.py::test_an_abandoned_screening_resumes_inside_the_window_only``;
* after the window a new screening starts (``test_expired_abandoned_session_starts_fresh``,
  ``test_call_back_after_the_resume_window_starts_over``): still valid, the scenario below adds
  that it must be the *same candidate*;
* two lines bound to two processes keep two screenings
  (``test_channels_bound_to_different_processes_keep_separate_screenings``): same, plus identity below.

Changed in place because they pinned the behaviour this suite wants gone:
``test_final_decision_is_final_new_conversation_starts`` (now
``test_a_decided_candidate_who_writes_again_is_not_screened_again``, unit + legacy copy) and
``test_stories.py::test_a_final_decision_is_not_reopened_from_another_channel``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.analytics import metrics
from backend.backoffice.service import Invalid
from backend.config import settings
from backend.reengagement import ReengagementMonitor
from backend.storage import db
from tests.support.identity import (DEFERRED_ANALYTICS, assert_same_candidate, construction, construction_underage,
                                    courier, courier_no_license, is_opening, live_sessions_for, place_call,
                                    send_whatsapp, sessions_for, total_candidates, user_lines)

LAURA = "+34600111222"
PEDRO = "+34600222333"


def qualified_on_whatsapp(channel, number=LAURA) -> str:
    send_whatsapp(channel, number, "hola", *courier())
    [row] = sessions_for(number)
    assert row["status"] == "qualified", row
    return row["id"]


def disqualified_on_whatsapp(channel, number=PEDRO) -> str:
    send_whatsapp(channel, number, "hola", *courier_no_license())
    [row] = sessions_for(number)
    assert row["status"] == "disqualified", row
    return row["id"]


# ---------- a decided candidate gets in touch again ----------

def test_a_qualified_candidate_who_calls_is_not_screened_again(whatsapp, voice):
    qualified = qualified_on_whatsapp(whatsapp)

    action = voice.start_call(LAURA, "CA-again")

    assert not is_opening(action.text), f"a qualified candidate was screened again by phone: {action.text!r}"
    assert [r["id"] for r in sessions_for(LAURA)] == [qualified]
    assert db.get_session(qualified)["status"] == "qualified"


@pytest.mark.parametrize("decided", ["qualified", "disqualified"])
def test_the_reply_to_a_recruiter_reaches_the_recruiter(whatsapp, bo, decided):
    """The recruiter writes to a decided candidate (interview, another job offer...) and the candidate answers."""
    if decided == "qualified":
        number, session_id = LAURA, qualified_on_whatsapp(whatsapp)
    else:
        number, session_id = PEDRO, disqualified_on_whatsapp(whatsapp)
    bo.send_message(session_id, "Hola, ¿puedes venir el martes a las 10 a una entrevista?", by="Ana")

    replies = send_whatsapp(whatsapp, number, "sí, el martes a las 10 me viene bien")

    assert not any(is_opening(r) for r in replies), f"the candidate got a new screening instead: {replies!r}"
    assert replies == []  # the recruiter owns the conversation: the assistant stays silent
    assert [r["id"] for r in sessions_for(number)] == [session_id]
    assert user_lines(session_id)[-1] == "sí, el martes a las 10 me viene bien"


def test_a_candidate_in_a_recruiter_conversation_who_calls_gets_the_recruiter_message(whatsapp, voice, bo):
    session_id = qualified_on_whatsapp(whatsapp)
    bo.send_message(session_id, "Laura, llámanos cuando puedas para la entrevista", by="Ana")

    action = voice.start_call(LAURA, "CA-callback")

    assert not is_opening(action.text), f"the caller was screened again: {action.text!r}"
    assert action.session_id == session_id


# ---------- a knocked-out candidate applies again ----------

@pytest.mark.parametrize("profile_id, knockout, second_try", [
    ("delivery_courier", courier_no_license("Pedro Sánchez Gil"), courier("Pedro Sánchez Gil")),
    ("construction_laborer", construction_underage("Lucía Romero Gil"), construction("Lucía Romero Gil")),
], ids=["courier_no_license", "construction_underage"])
def test_a_knocked_out_candidate_who_applies_again_is_the_same_candidate(lines, profile_id, knockout, second_try):
    """Whatever the re-application policy (cooldown, re-ask the knockout, a new application), the
    recruiter must never see the second try as an unrelated person, and the knockout must stand."""
    line = lines("whatsapp", profile_id)
    send_whatsapp(line, PEDRO, "hola", *knockout)
    [first] = sessions_for(PEDRO)
    assert first["status"] == "disqualified"

    send_whatsapp(line, PEDRO, "hola", *second_try)

    assert db.get_session(first["id"])["status"] == "disqualified"  # the decision stands
    assert_same_candidate(*[r["id"] for r in sessions_for(PEDRO)])


# ---------- coming back later / another process ----------

def test_coming_back_after_the_resume_window_is_the_same_candidate(whatsapp, store, agent):
    send_whatsapp(whatsapp, LAURA, "hola", *courier()[:2])
    state = store.find_by_contact("whatsapp", LAURA)
    agent.end(state, reason="silence")
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(hours=settings.resume_window_hours + 24)
    db.save_session(state)

    send_whatsapp(whatsapp, LAURA, "hola, sigo interesada")

    rows = sessions_for(LAURA)
    assert len(rows) == 2  # a new screening is fine after the window...
    assert_same_candidate(*[r["id"] for r in rows])  # ... but it is the same person


def test_applications_to_two_processes_are_the_same_candidate(lines):
    send_whatsapp(lines("whatsapp", "delivery_courier"), LAURA, "hola", *courier()[:3])
    place_call(lines("voice", "construction_laborer"), LAURA, "CA-2nd-job", *construction("Laura Jiménez Ortiz")[:2])

    rows = sessions_for(LAURA)
    assert sorted(r["profile_id"] for r in rows) == ["construction_laborer", "delivery_courier"]
    assert_same_candidate(*[r["id"] for r in rows])


@DEFERRED_ANALYTICS
def test_analytics_count_people_not_sessions(lines):
    send_whatsapp(lines("whatsapp", "delivery_courier"), LAURA, "hola", *courier()[:3])
    place_call(lines("voice", "construction_laborer"), LAURA, "CA-2nd-job", *construction("Laura Jiménez Ortiz")[:2])

    analytics = metrics.compute()

    assert analytics["total_sessions"] == 2
    assert total_candidates(analytics) == 1


def test_reopening_an_old_screening_while_a_newer_one_is_live(whatsapp, bo):
    """A recruiter reopens the knockout of a candidate who has already started over: the answer must
    land in the screening that asked the question, never in another one. Refusing the reopen is fine."""
    old = disqualified_on_whatsapp(whatsapp)
    send_whatsapp(whatsapp, PEDRO, "hola", *courier_no_license()[:2])  # started over: now at the licence question

    try:
        bo.reopen(old, by="Ana", question_id="license")
    except Invalid:
        return  # refused: the recruiter is pointed at the live screening instead
    send_whatsapp(whatsapp, PEDRO, "sí, ya tengo carnet")

    live = live_sessions_for(PEDRO)
    assert len(live) == 1, f"one candidate with {len(live)} live screenings of the same process: {live}"
    assert "sí, ya tengo carnet" in user_lines(live[0]["id"])
    assert live[0]["stage"] == "location"


def test_a_deactivated_process_is_not_continued_on_whatsapp(agent, lines, monkeypatch):
    """WHATSAPP_PROFILE moves to another process and the old one is deactivated: the web chat already
    refuses inactive processes; WhatsApp must not keep screening for it either."""
    send_whatsapp(lines("whatsapp", "delivery_courier"), LAURA, "hola", *courier()[:2])
    [courier_session] = sessions_for(LAURA)
    before = len(db.get_session(courier_session["id"])["messages"])
    monkeypatch.setattr(agent.profiles.get("delivery_courier"), "active", False)

    replies = send_whatsapp(lines("whatsapp", "construction_laborer"), LAURA, "hola, ¿seguimos?")

    assert len(db.get_session(courier_session["id"])["messages"]) == before, (
        f"the candidate kept being screened for a deactivated process: {replies!r}")


# ---------- screenings nobody answered ----------

def _unanswered_call(voice, store, days_ago: int) -> str:
    """A caller who hung up during the opening, ``days_ago`` days ago."""
    action = voice.start_call(LAURA, "CA-hangup")
    voice.call_ended("CA-hangup", "completed")
    state = store.get(action.session_id)
    state.started_at -= timedelta(days=days_ago)
    state.last_candidate_ts -= timedelta(days=days_ago)
    db.save_session(state)
    return state.id


def test_a_candidate_coming_back_months_later_is_not_continued_in_a_months_old_screening(voice, whatsapp, store):
    """A screening pins the process revision it started with: a months-old one would screen with
    rules that may no longer be published."""
    old = _unanswered_call(voice, store, days_ago=120)

    send_whatsapp(whatsapp, LAURA, "hola, he visto la oferta")

    handled_by = [r for r in sessions_for(LAURA) if "hola, he visto la oferta" in user_lines(r["id"])]
    assert handled_by and handled_by[0]["id"] != old, "the message went into a 120-day-old screening"


def test_a_screening_nobody_answered_is_not_in_conversation_forever(voice, store, agent):
    """The monitor skips sessions without a candidate message, so they stay "active" (and count as
    "in conversation" in the analytics) for ever."""
    old = _unanswered_call(voice, store, days_ago=120)

    ReengagementMonitor(agent, store.registry).tick()

    assert db.get_session(old)["status"] != "active"


# ---------- analytics at volume ----------

@DEFERRED_ANALYTICS
def test_analytics_are_not_capped_at_5000_sessions(temp_db):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO sessions (id, profile_id, status, stage, started_at, last_ts) VALUES (?,?,?,?,?,?)",
            [(f"s{i:05d}", "delivery_courier", "abandoned", "abandoned", now, now) for i in range(5001)])

    assert metrics.compute()["total_sessions"] == 5001
