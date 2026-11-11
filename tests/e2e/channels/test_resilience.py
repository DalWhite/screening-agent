"""Channels when things go wrong: restarts and crashes, provider retries and outages,
slow turns, the 24h window, tunnels and misconfiguration, real-time re-engagement,
and many candidates at once.

These need a live stack (a real uvicorn process, real background workers, real
time), so they live in the e2e suite. Tests that change a stack's configuration
or restart it use their own stack from the pool.
"""

from __future__ import annotations

import concurrent.futures
import random
import time
from collections import Counter

import pytest
from playwright.sync_api import Page, expect

from backend.integrations.registry import providers
from simulators.testing import conversation_as_lived, conversation_as_stored, redacted
from tests.support.candidates import construction_answers, unique_phone, unique_surname
from tests.support.channels import CONSTRUCTION, assert_nothing_lost, only_session, open_candidate


def wait_for_event(stack, session_id: str, event_type: str, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in stack.session(session_id)["events"]:
            if event["type"] == event_type:
                return event
        time.sleep(0.1)
    raise AssertionError(f"no {event_type} event for {session_id}")


# ---------- restarts and crashes ----------

@pytest.mark.parametrize("provider", ["twilio", "meta"])
@pytest.mark.parametrize("crash", [False, True], ids=["deploy", "crash"])
def test_whatsapp_conversation_survives_an_app_restart(stacks, provider, crash):
    stack = stacks.get(provider, **CONSTRUCTION, STACK="restarts")
    name = f"Raúl {unique_surname()}"
    answers = construction_answers(name)
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    for text in answers[:4]:
        phone.whatsapp(text)
    stack.restart_app(crash=crash)
    assert "ciudad" in phone.whatsapp(answers[4])[0]  # same screening, next question
    for text in answers[5:]:
        phone.whatsapp(text)
    session = assert_nothing_lost(stack, phone)
    assert stack.wait_for_status(session["id"])["status"] == "qualified"


@pytest.mark.parametrize("voice", providers("voice"))
def test_a_call_survives_an_app_restart_between_two_answers(stacks, voice):
    stack = stacks.get("twilio", voice=voice, **CONSTRUCTION, STACK="restarts")
    name = f"Óscar {unique_surname()}"
    call = stack.sim.phone(unique_phone(), name).call()
    call.say("sí")
    stack.restart_app()  # a deploy while the caller is thinking
    call.say(name)
    assert "18" in call.prompt  # Twilio's next webhook reached the new process: same call, same screening
    call.hangup()
    stack.sim.flush()
    events = [e["type"] for e in only_session(stack, call.phone)["events"]]
    assert "voice_call_ended" in events  # the status callback still finds its session


def test_a_provider_redelivery_after_a_crash_is_processed_once(stacks):
    """Meta retries webhooks it has no 200 for: after a crash the same message can come again."""
    stack = stacks.get("meta", **CONSTRUCTION, STACK="restarts")
    phone = stack.sim.phone(unique_phone())
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    stack.restart_app(crash=True)
    assert phone.resend_last() == []  # already answered before the crash
    user_lines = [m["content"] for m in only_session(stack, phone)["messages"] if m["role"] == "user"]
    assert user_lines == ["hola", "sí"]


@pytest.mark.parametrize("voice", providers("voice"))
def test_calls_during_downtime_fail_then_work_again(stacks, voice):
    stack = stacks.get("twilio", voice=voice, **CONSTRUCTION, STACK="restarts")
    phone = stack.sim.phone(unique_phone())
    stack.app_process.kill()
    try:
        call = phone.call()
        assert call.end_reason == "application-error"  # the provider apologises and hangs up
        assert phone.whatsapp("hola") == []  # and the WhatsApp message gets no answer (11200)
        assert stack.sim.log[-1].error.startswith("11200")
    finally:
        stack.app_process.start()
    assert "Constructora Horizonte" in phone.call().prompt


# ---------- the provider misbehaves ----------

def test_outage_queues_recruiter_messages_until_the_candidate_writes(page: Page, live):
    name = f"Marta {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    session_id = only_session(live, phone)["id"]
    open_candidate(page, live, session_id)

    live.sim.faults.rest_down = True  # Twilio / Meta API down
    page.get_by_label("Mensaje al candidato").fill("Marta, ¿puedes venir el lunes a las 8?")
    page.get_by_test_id("send-message").click()
    pending = page.get_by_test_id("msg-recruiter").last.get_by_text("pendiente de entrega")
    expect(pending).to_be_visible()
    live.sim.faults.rest_down = False

    assert phone.whatsapp("perdona, ¿qué me decías?")[0] == "Marta, ¿puedes venir el lunes a las 8?"
    expect(pending).to_have_count(0, timeout=15_000)


def test_a_message_outside_the_24h_window_goes_out_as_a_template(page: Page, live):
    name = f"Clara {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    phone.whatsapp(name)
    session_id = only_session(live, phone)["id"]
    phone.close_window()  # her last message was yesterday
    live.sim.faults.window_errors = "async"  # accepted, then failed (Meta always works like this)

    open_candidate(page, live, session_id)
    page.get_by_label("Mensaje al candidato").fill("Clara, ¿sigues interesada en el puesto?")
    page.get_by_test_id("send-message").click()
    template = phone.wait_for_whatsapp(lambda m: m.via == "template", timeout=15)
    assert template.body.startswith(f"Hola {name.split()[0]}, soy Andrés")
    wait_for_event(live, session_id, "whatsapp_template_fallback")  # recorded right after the send returns

    page.reload()
    page.get_by_test_id("tab-activity").click()
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("WhatsApp no pudo entregar un mensaje", timeout=15_000)
    expect(timeline).to_contain_text("Enviado como plantilla de WhatsApp")
    assert phone.whatsapp("sí, dime")[0] == "Clara, ¿sigues interesada en el puesto?"  # she answers: it arrives


def test_meta_development_number_can_only_write_to_verified_phones(page: Page, channels_meta):
    stack = channels_meta
    phone = stack.sim.phone(unique_phone())
    phone.whatsapp("hola")
    stack.sim.faults.recipient_allow_list = ["+34999999999"]  # the tester forgot to verify this phone
    session_id = only_session(stack, phone)["id"]
    sent = stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "¿Seguimos?"}).json()
    assert sent["delivered"] is False
    assert any((e.error or "").startswith("131030") for e in stack.sim.log)


def test_voice_notes_when_speech_to_text_is_down(page: Page, live):
    name = f"Lola {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    live.sim.faults.stt_down = True
    [reply] = phone.voice_note(name)
    assert reply.startswith("No he podido entender tu nota de voz")
    live.sim.faults.stt_down = False
    assert "18" in phone.voice_note(name)[0]
    session_id = only_session(live, phone)["id"]
    open_candidate(page, live, session_id)
    notes = page.get_by_test_id("transcript").locator(".msg.kind-voice_note")
    expect(notes.first.get_by_test_id("msg-kind")).to_contain_text("sin transcribir")
    expect(notes.last).to_contain_text(name)


@pytest.mark.parametrize("provider", ["twilio", "meta"])
def test_slow_turns_are_answered_through_the_api(stacks, provider):
    """An LLM slower than the webhook budget: every answer is pushed, in order, nothing lost."""
    stack = stacks.get(provider, **CONSTRUCTION, WHATSAPP_SYNC_TIMEOUT_SECONDS="0")
    name = f"Abel {unique_surname()}"
    phone = stack.sim.phone(unique_phone(), name)
    assert phone.whatsapp("hola") == []
    phone.wait_for_whatsapp("Constructora Horizonte")
    for text, expected in zip(construction_answers(name)[:3], ["nombre", "18", "permiso"]):
        mark = len(phone.thread)
        assert phone.whatsapp(text) == []
        assert expected in phone.wait_for_whatsapp(since=mark).body.lower()
    assert all(m.pushed for m in phone.received())
    assert_nothing_lost(stack, phone)


# ---------- deployment: tunnels and misconfiguration ----------

def test_behind_a_tunnel_signatures_use_the_public_url(stacks):
    """Twilio signs the URL it calls (the tunnel's), not the app's local address."""
    public = "https://screening-e2e.ngrok.test"
    stack = stacks.get("twilio", sim_kwargs={"public_url": public}, PUBLIC_BASE_URL=public, **CONSTRUCTION)
    phone = stack.sim.phone(unique_phone())
    assert "Constructora Horizonte" in phone.whatsapp("hola")[0]
    call = phone.call()
    assert call.waiting and call.say("sí")  # the <Gather action> is an absolute tunnel URL
    assert all(e.url.startswith(public) for e in stack.sim.log if e.kind in ("webhook", "status_callback"))


def test_a_wrong_public_base_url_rejects_twilio_but_not_meta(stacks):
    """The first thing to check when 'messages arrive but nothing answers'."""
    twilio = stacks.get("twilio", PUBLIC_BASE_URL="https://old-tunnel.example", **CONSTRUCTION)
    phone = twilio.sim.phone(unique_phone())
    assert phone.whatsapp("hola") == []
    assert twilio.sim.log[-1].status == 403 and twilio.sessions_for(phone) == []
    assert phone.call().end_reason == "application-error"

    meta = stacks.get("meta", PUBLIC_BASE_URL="https://old-tunnel.example", **CONSTRUCTION)
    assert "Constructora Horizonte" in meta.sim.phone(unique_phone()).whatsapp("hola")[0]  # Meta signs the body


# ---------- real-time re-engagement ----------

FAST_CLOCK = {**CONSTRUCTION, "REENGAGE_TICK_SECONDS": "0.3", "REENGAGE_FIRST_DELAY_SECONDS": "2",
              "REENGAGE_SECOND_DELAY_SECONDS": "4", "SESSION_IDLE_SECONDS": "6"}


@pytest.mark.parametrize("provider", ["twilio", "meta"])
def test_a_silent_whatsapp_candidate_is_nudged_closed_and_can_come_back(page: Page, stacks, provider):
    stack = stacks.get(provider, **FAST_CLOCK)
    name = f"Hugo {unique_surname()}"
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    first = phone.wait_for_whatsapp(lambda m: m.pushed, timeout=10)
    second = phone.wait_for_whatsapp(lambda m: m.pushed and m is not first, timeout=10)
    assert "sigues" in first.body.lower() and second.body != first.body
    session_id = only_session(stack, phone)["id"]
    assert "silence" in stack.wait_for_status(session_id, "abandoned", timeout=15)["flags"]

    replies = phone.whatsapp(name)  # she comes back, with the answer
    assert "de nuevo" in replies[0].lower() and "18" in replies[-1]
    open_candidate(page, stack, session_id, tab="activity")
    expect(page.get_by_test_id("timeline")).to_contain_text("Recordatorio automático #2")
    expect(page.get_by_test_id("timeline")).to_contain_text("El candidato retomó la conversación")


def test_nudges_queued_for_a_caller_are_not_replayed_when_they_write(stacks):
    """A phone candidate cannot be nudged (no push on voice). When they later write on WhatsApp,
    stale "are you still there?" messages must not be the first thing they read."""
    stack = stacks.get("twilio", **FAST_CLOCK)
    phone = stack.sim.phone(unique_phone())
    call = phone.call()
    call.say("sí")
    call.hangup()
    session_id = only_session(stack, phone)["id"]
    deadline = time.time() + 10
    while stack.session(session_id)["reengage_attempts"] < 2 and time.time() < deadline:
        time.sleep(0.2)
    replies = phone.whatsapp("hola, perdona")
    assert not any("sigues" in r.lower() or "dejo la conversación" in r.lower() for r in replies), replies
    assert "de nuevo" in replies[0].lower()


# ---------- many candidates, both channels at once ----------

@pytest.mark.parametrize("provider", ["twilio", "meta"])
def test_many_candidates_on_both_channels_at_the_same_time(stacks, provider):
    stack = stacks.get(provider, **CONSTRUCTION)
    rng = random.Random(7)

    def journey(i: int):
        name = f"Cand{chr(65 + i)} {unique_surname()}"
        answers = construction_answers(name)
        phone = stack.sim.phone(unique_phone(), name)
        switch_at = rng.randint(2, 9)
        route = rng.choice(["whatsapp", "voice", "whatsapp-voice", "voice-whatsapp"])
        first, second = (route.split("-") + [None])[:2]

        def run(channel, texts, call=None):
            if channel == "voice":
                call = phone.call()
                for text in texts:
                    call.say(text)
                if call.active:
                    call.hangup()
            else:
                if not phone.thread:
                    phone.whatsapp("hola")
                for text in texts:
                    phone.whatsapp(text)

        run(first, answers[:switch_at] if second else answers)
        if second:
            run(second, answers[switch_at:])
        return phone, name

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(journey, range(12)))
    for phone, name in results:
        session = assert_nothing_lost(stack, phone, ordered=False)  # nothing lost, no cross-talk
        session = stack.wait_for_status(session["id"], timeout=20)
        assert session["status"] == "qualified", (name, session["status"], session["stage"])
        assert session["data"]["full_name"] == name  # no cross-talk between candidates


def test_the_same_candidate_on_whatsapp_and_on_the_phone_at_once(stacks):
    stack = stacks.get("twilio", **CONSTRUCTION)
    name = f"Dúo {unique_surname()}"
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    call = phone.call()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        spoken = pool.submit(call.say, name)
        written = pool.submit(phone.whatsapp, name)
        spoken.result(), written.result()
    call.hangup()
    session = only_session(stack, phone)
    assert session["data"]["full_name"] == name and session["stage"] in ("adult", "work_permit")
    assert sum(1 for m in session["messages"] if m["role"] == "user" and name in m["content"]) == 2
    # both turns really happened at once: every line is stored, in whichever order the app took them
    deadline = time.time() + 5
    while True:
        stack.sim.flush()
        stored = Counter(conversation_as_stored(stack.session(session["id"])))
        lived = Counter(redacted(conversation_as_lived(phone)))
        if stored == lived or time.time() > deadline:
            break
        time.sleep(0.1)
    assert stored == lived
