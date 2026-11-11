"""The conversation registry: everything a candidate says, reads or hears is stored, in order,
with its channel, whatever channel switches happen mid-screening.

The simulator is the source of truth for what the candidate lived (its WhatsApp
thread and call transcripts); the backend transcript must match it line for
line. Every test runs on both WhatsApp providers (Twilio, Meta Cloud API).
"""

from __future__ import annotations

import pytest

from backend.config import settings
from simulators.testing import conversation_as_lived, conversation_as_stored, redacted

CARLOS = "+34600333444"
PROCESS = {"whatsapp_profile": "construction_laborer", "voice_profile": "construction_laborer"}


def test_a_cross_channel_conversation_is_recorded_line_for_line(channel_stack, wa_provider, voice_provider):
    stack = channel_stack(provider=wa_provider, voice=voice_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS, "Carlos")

    for text in ["hola", "sí", "Carlos Martín Ruiz"]:
        carlos.whatsapp(text)
    carlos.voice_note("sí, tengo 34 años")  # a voice note answers the age question
    carlos.photo()  # a photo of something: not stored, politely declined
    call = carlos.call()  # WhatsApp -> phone
    call.say("sí, tengo NIE con permiso")
    call.silence()  # the re-prompt is part of the conversation too
    call.say("mi DNI es 12345678Z")
    session_id = stack.session_for(carlos)["id"]
    stack.api.post(f"/api/bo/candidates/{session_id}/takeover", json={"paused": True})
    call.say("oye, ¿hay alguien?")  # answered with the takeover notice
    stack.api.post(f"/api/bo/candidates/{session_id}/takeover", json={"paused": False})
    carlos.wait_for_whatsapp("Sigo yo")  # hand-back pushed on WhatsApp while still on the call
    call.hangup()
    carlos.whatsapp("Vivo en Getafe")  # phone -> WhatsApp, and the first message is an answer
    stack.sim.flush()

    session = stack.session(session_id)
    assert conversation_as_stored(session) == redacted(conversation_as_lived(carlos))
    assert session["data"]["zone"] == "Getafe"  # the answer that came with the switch was not lost

    kinds = {(m["channel"], m["kind"]) for m in session["messages"]}
    assert {("whatsapp", "voice_note"), ("whatsapp", "media"), ("voice", "speech"), ("voice", "reprompt"),
            ("voice", "notice"), ("whatsapp", "notice")} <= kinds


def test_the_answer_that_comes_with_a_switch_is_processed(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    call = carlos.call()
    for text in ["sí", "Carlos Martín Ruiz", "sí", "sí", "12345678Z"]:
        call.say(text)
    assert "ciudad" in call.prompt
    call.hangup()

    replies = carlos.whatsapp("Vivo en Getafe")
    assert replies[0] == "¡Hola de nuevo! Seguimos donde lo dejamos."
    assert "físico" in replies[-1]  # next question: the city was taken
    session = stack.session_for(carlos)
    assert session["data"]["zone"] == "Getafe" and session["stage"] == "physical"


def test_hello_after_a_switch_gets_the_pending_question_without_burning_an_attempt(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    call = carlos.call()
    call.say("sí")
    pending = call.prompt
    call.hangup()

    [greeting] = carlos.whatsapp("hola, se me cortó la llamada")
    assert "de nuevo" in greeting.lower() and pending in greeting
    state = stack.app.store.get(stack.session_for(carlos)["id"])
    assert state.attempts_for(state.stage) == 0


def test_call_lines_carry_speech_details(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider, **PROCESS)
    call = stack.sim.phone(CARLOS).call()
    call.say("sí", confidence=0.61)
    call.silence()
    messages = stack.session_for(CARLOS)["messages"]
    speech = next(m for m in messages if m["kind"] == "speech")
    assert speech["channel"] == "voice" and speech["meta"] == {"call_sid": call.sid, "confidence": 0.61}
    reprompt = next(m for m in messages if m["kind"] == "reprompt")
    assert reprompt["meta"] == {"attempt": 1} and "sigues" in reprompt["content"].lower()


def test_webhook_retries_are_processed_once(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    for text in ["hola", "sí"]:
        carlos.whatsapp(text)
    assert carlos.resend_last() == []  # same message id again: ignored, not answered twice
    user_lines = [m["content"] for m in stack.session_for(carlos)["messages"] if m["role"] == "user"]
    assert user_lines == ["hola", "sí"]


def test_queued_messages_are_recorded_on_the_channel_that_finally_delivered_them(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    call = carlos.call()
    call.say("sí")
    call.hangup()
    session_id = stack.session_for(carlos)["id"]
    # the candidate only ever called: the recruiter's message cannot be pushed yet
    sent = stack.api.post(f"/api/bo/candidates/{session_id}/messages",
                          json={"text": "Carlos, te he dejado un mensaje", "pause_bot": False}).json()
    assert sent["delivered"] is False
    queued = stack.session(session_id)["messages"][-1]
    assert queued["role"] == "recruiter" and queued["channel"] is None and not queued["delivered"]

    replies = carlos.whatsapp("hola")
    assert replies[0] == "Carlos, te he dejado un mensaje"
    delivered = next(m for m in stack.session(session_id)["messages"] if m["role"] == "recruiter")
    assert delivered["delivered"] and delivered["channel"] == "whatsapp"


def test_the_backoffice_shows_channel_and_kind_of_every_message(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    carlos.whatsapp("hola")
    carlos.voice_note("sí, empecemos")
    candidate = stack.candidate(stack.session_for(carlos)["id"])
    note = next(m for m in candidate["messages"] if m["kind"] == "voice_note")
    assert note["channel"] == "whatsapp" and note["content"] == "sí, empecemos"
    assert note["meta"]["transcribed"] is True and note["meta"]["audio_stored"] is False
    assert settings.whatsapp_media_max_bytes > note["meta"]["bytes"] > 0


@pytest.mark.parametrize("small_talk", ["perdona, no te oía bien", "no sé si me oyes", "hola, perdona, no podía hablar"])
def test_small_talk_after_a_switch_never_answers_a_knockout_question(channel_stack, wa_provider, small_talk):
    """At "¿Eres mayor de 18 años?" a stray "no" used to reject the candidate (a legal knockout)."""
    stack = channel_stack(provider=wa_provider, **PROCESS)
    carlos = stack.sim.phone(CARLOS)
    call = carlos.call()
    call.say("sí")
    call.say("Carlos Martín Ruiz")
    assert "18" in call.prompt
    call.hangup()

    replies = carlos.whatsapp(small_talk)
    assert "18" in replies[-1]  # asked again
    state = stack.app.store.get(stack.session_for(carlos)["id"])
    assert state.status == "active" and state.attempts_for("is_adult") == 0  # no knockout, no try spent
    assert "permiso" in carlos.whatsapp("sí, tengo 34")[-1]


def test_a_caller_who_did_not_hear_the_question_is_not_rejected(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider, **PROCESS)
    call = stack.sim.phone(CARLOS).call()
    call.say("sí")
    call.say("Carlos Martín Ruiz")
    call.say("perdona, no te he oído bien")
    assert "18" in call.prompt and stack.session_for(CARLOS)["status"] == "active"
