"""Recruiters working with WhatsApp and phone candidates, in the browser.

The candidate side is the simulator (real HTTP, both WhatsApp providers via
``live``); the recruiter side is the backoffice in Chromium. Every test is a
two-sided story: what the recruiter does reaches the candidate's channel, and
what the candidate does shows up for the recruiter without reloading.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from tests.support.candidates import ERROR_ID, construction_answers, unique_phone, unique_surname
from tests.support.channels import ensure_profile, only_session, open_candidate


def start(stack, name: str, answered: int):
    """A WhatsApp candidate who has answered the first ``answered`` questions."""
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    for text in construction_answers(name)[:answered]:
        phone.whatsapp(text)
    return phone, only_session(stack, phone)["id"]


def test_the_transcript_follows_the_candidate_live(page: Page, live):
    name = f"Nuria {unique_surname()}"
    phone, session_id = start(live, name, answered=2)
    open_candidate(page, live, session_id)
    transcript = page.get_by_test_id("transcript")
    expect(transcript).to_contain_text(name)

    phone.voice_note("sí, tengo 34 años")  # no reload: the page polls
    expect(transcript.locator(".msg.kind-voice_note")).to_contain_text("sí, tengo 34 años", timeout=15_000)
    call = phone.call()
    call.say("sí, tengo NIE con permiso")
    expect(transcript.get_by_test_id("channel-divider").last).to_have_text("Teléfono", timeout=15_000)
    expect(transcript.locator(".msg.kind-speech")).to_contain_text("Sí, tengo NIE con permiso.")
    call.hangup()
    expect(page.locator(".cand-sub")).to_contain_text("WhatsApp → Teléfono")


def test_quick_reply_from_the_pipeline_reaches_whatsapp(page: Page, live):
    name = f"Iván {unique_surname()}"
    phone, _ = start(live, name, answered=3)
    page.goto(f"{live.url}/backoffice#/pipeline?q={quote(name)}")
    page.get_by_test_id("candidate-card").filter(has_text=name).click()
    quick = page.get_by_test_id("quick-view")
    quick.get_by_label("Respuesta rápida").fill("Iván, ¿te viene bien pasarte el jueves por la obra?")
    quick.get_by_role("button", name="Enviar respuesta").click()
    assert phone.wait_for_whatsapp("jueves por la obra").pushed
    assert phone.whatsapp("sí, el jueves me va bien") == []  # the recruiter took over: the assistant listens


def test_a_candidate_who_calls_during_a_takeover_leaves_a_message(page: Page, live):
    name = f"Elena {unique_surname()}"
    phone, session_id = start(live, name, answered=2)
    open_candidate(page, live, session_id)
    page.get_by_role("button", name="Tomar el control").click()
    expect(page.get_by_test_id("takeover-banner")).to_contain_text("Estás atendiendo esta conversación")

    call = phone.call()
    assert "te contactará pronto" in call.prompt and "18" not in call.prompt  # no screening question
    call.say("quería saber si el contrato es de jornada completa")
    assert "se lo paso al reclutador" in call.prompt
    call.silence()  # nothing more to say
    assert not call.active and "te contactará pronto" in call.agent_lines()[-1]

    transcript = page.get_by_test_id("transcript")
    expect(transcript.locator(".msg.kind-speech")).to_contain_text("jornada completa", timeout=15_000)
    assert live.session(session_id)["status"] == "active"  # silence did not close it under the recruiter's feet

    mark = len(phone.thread)
    page.get_by_role("button", name="Devolver al asistente").click()
    handback = phone.wait_for_whatsapp(since=mark)
    assert "18" in handback.body  # the assistant resumes with the pending question, on WhatsApp


def test_a_message_to_a_phone_only_candidate_waits_for_whatsapp(page: Page, live):
    name = f"Tomás {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    call = phone.call()
    for text in construction_answers(name)[:3]:
        call.say(text)
    call.hangup()
    session_id = only_session(live, phone)["id"]

    open_candidate(page, live, session_id)
    expect(page.get_by_text("Habló por teléfono: el mensaje le llega si escribe por WhatsApp.")).to_be_visible()
    page.get_by_label("Mensaje al candidato").fill("Tomás, te escribo por aquí: ¿tienes el NIE a mano?")
    page.get_by_test_id("send-message").click()
    recruiter = page.get_by_test_id("msg-recruiter").last
    expect(recruiter.get_by_text("pendiente de entrega")).to_be_visible()

    replies = phone.whatsapp("hola")  # the candidate writes on WhatsApp for the first time
    assert replies[0] == "Tomás, te escribo por aquí: ¿tienes el NIE a mano?"
    expect(recruiter.get_by_text("pendiente de entrega")).to_have_count(0, timeout=15_000)
    assert phone.whatsapp("sí, lo tengo") == []  # still the recruiter's conversation


@pytest.mark.parametrize("provider", ["twilio", "meta"])
def test_manual_id_check_decided_while_the_candidate_waits_on_whatsapp(page: Page, stacks, provider):
    process = "construction_manual_e2e"
    stack = stacks.get(provider, WHATSAPP_PROFILE=process, VOICE_PROFILE=process)

    def manual(data):
        data["checks"][0]["provider"] = "manual"

    ensure_profile(stack, process, "construction_laborer", manual)
    name = f"Pablo {unique_surname()}"
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    last = [phone.whatsapp(text) for text in construction_answers(name)][-1]
    assert "verificando" in last[0].lower()
    assert "verificando" in phone.whatsapp("¿y ahora qué?")[0].lower()  # still waiting: same answer

    session_id = only_session(stack, phone)["id"]
    open_candidate(page, stack, session_id, tab="screening")
    row = page.get_by_test_id("checks-table").locator("tr[data-check='id_check']")
    expect(row.get_by_test_id("check-status")).to_have_text("Pendiente")
    row.get_by_role("button", name="Marcar Verificación de identidad como válida").click()
    expect(row.get_by_test_id("check-status")).to_have_text("Válido")

    decision = phone.wait_for_whatsapp(lambda m: m.pushed and "cumples" in m.body.lower(), timeout=15)
    assert decision.via in ("rest", "graph")


def test_reopening_a_rejected_whatsapp_candidate(page: Page, live):
    name = f"Lucía {unique_surname()}"
    phone, session_id = start(live, name, answered=2)
    assert "mayor de edad" in phone.whatsapp("tengo 17 años")[0]
    open_candidate(page, live, session_id)
    mark = len(phone.thread)
    page.get_by_test_id("reopen").click()
    expect(page.locator(".toast").last).to_contain_text("Screening reabierto")
    reopened = phone.wait_for_whatsapp(since=mark)
    assert "18" in reopened.body  # the knockout question, asked again on WhatsApp
    assert "permiso" in phone.whatsapp("perdón, me equivoqué: tengo 19 años")[0]
    assert live.session(session_id)["status"] == "active"


def test_registry_failure_resolved_by_hand_tells_the_whatsapp_candidate(page: Page, live):
    name = f"Iker {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    for text in construction_answers(name, ERROR_ID):
        phone.whatsapp(text)
    session_id = only_session(live, phone)["id"]
    live.wait_for_status(session_id)
    open_candidate(page, live, session_id, tab="screening")
    row = page.get_by_test_id("checks-table").locator("tr[data-check='id_check']")
    expect(row.get_by_test_id("check-status")).to_have_text("Error", timeout=15_000)
    row.get_by_role("button", name="Marcar Verificación de identidad como válida").click()
    expect(page.locator(".cand-actions").get_by_test_id("stage-pill")).to_contain_text("Pre-calificado")
    expect(page.locator(".flag-list")).not_to_contain_text("Verificación fallida")


def test_searching_channel_candidates_by_phone_number(page: Page, live):
    name = f"Aitana {unique_surname()}"
    phone, _ = start(live, name, answered=2)
    digits = phone.number[3:]
    spaced = f"{digits[:3]} {digits[3:5]} {digits[5:7]} {digits[7:]}"  # how a recruiter types it
    page.goto(f"{live.url}/backoffice#/candidates")
    page.get_by_test_id("search").fill(spaced)
    rows = page.get_by_test_id("candidate-row")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(name)

    with page.expect_download() as download:
        page.get_by_test_id("export-csv").click()
    csv = download.value.path().read_text(encoding="utf-8-sig")
    line = next(row for row in csv.splitlines() if name in row)
    assert phone.number in line and ",whatsapp," in line
