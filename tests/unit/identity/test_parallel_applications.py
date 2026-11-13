"""One candidate applying to several processes at the same time (docs/identity_design.md §7).

Parallel applications are allowed by design: one live application per candidate *and process*.
Today they only happen when the two lines run two processes (WhatsApp: courier, phone:
construction), and the two screenings step on each other's toes.

Already written elsewhere: both applications belong to the same candidate
(``test_returning_candidates.py::test_applications_to_two_processes_are_the_same_candidate``).

Written here: two defects that exist whatever routing model is chosen. Scenarios that depend on
the routing decision (a shared WhatsApp number for several processes, focus switching, queueing a
second application) are listed in the design doc and written once it is decided.
"""

from __future__ import annotations

from backend.storage import db
from tests.support.identity import construction, courier, place_call, send_whatsapp, sessions_for, user_lines

LAURA = "+34600111222"


def _two_applications(lines):
    """Courier by WhatsApp (at the city question) and construction by phone (at the work-permit question)."""
    whatsapp_line = lines("whatsapp", "delivery_courier")
    phone_line = lines("voice", "construction_laborer")
    send_whatsapp(whatsapp_line, LAURA, "hola", *courier()[:3])
    place_call(phone_line, LAURA, "CA-obra", *construction("Laura Jiménez Ortiz")[:3])
    phone_line.call_ended("CA-obra", "completed")
    rows = {r["profile_id"]: r for r in sessions_for(LAURA)}
    assert {p: r["status"] for p, r in rows.items()} == {"delivery_courier": "active", "construction_laborer": "active"}
    return whatsapp_line, rows["delivery_courier"]["id"], rows["construction_laborer"]["id"]


def test_a_recruiter_message_about_one_application_reaches_the_candidate_on_whatsapp(lines, bo, agent,
                                                                                       fake_twilio_client):
    """The construction recruiter writes: the candidate only ever used WhatsApp for the courier job,
    but WhatsApp is how *the candidate* can be reached. Today it stays queued for ever (the WhatsApp
    line only delivers the courier application's queue). With two live applications the candidate
    must be able to tell which job it is about."""
    _, _, construction_id = _two_applications(lines)

    result = bo.send_message(construction_id, "Laura, necesito tu talla de botas", by="Eva")

    assert result["delivered"] is True, "the recruiter's message was not pushed"
    pushed = [m["body"] for m in fake_twilio_client.messages.sent if "talla de botas" in (m["body"] or "")]
    assert pushed, "nothing reached the candidate's WhatsApp"
    profile = agent.profiles.get("construction_laborer")
    labels = (profile.company, profile.role.get("es") if isinstance(profile.role, dict) else str(profile.role))
    assert any(label and label in pushed[-1] for label in labels), (
        f"two live applications, and the message does not say which job it is about: {pushed[-1]!r}")


def test_the_reply_to_that_recruiter_does_not_answer_the_other_screening(lines, bo):
    """Found while analysing parallel applications: the reply "la 42" (a boot size, for the
    construction recruiter) was taken by the courier screening as the candidate's city, leading to an
    out-of-area knockout in a process the recruiter never wrote about."""
    whatsapp_line, courier_id, construction_id = _two_applications(lines)
    before = db.get_session(courier_id)
    bo.send_message(construction_id, "Laura, necesito tu talla de botas", by="Eva")

    replies = send_whatsapp(whatsapp_line, LAURA, "la 42")

    assert "la 42" not in user_lines(courier_id), (
        f"the courier screening consumed a reply meant for the construction recruiter: {replies!r}")
    after = db.get_session(courier_id)
    assert (after["status"], after["stage"], after["data"]) == (before["status"], before["stage"], before["data"])
    assert user_lines(construction_id)[-1] == "la 42"
    assert replies == []  # the construction application is in a recruiter conversation: the assistant is silent
