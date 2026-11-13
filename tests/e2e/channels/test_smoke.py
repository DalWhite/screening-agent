"""WhatsApp and phone candidates, end to end, through the local Twilio simulator.

The candidate side is the simulator (signed webhooks, TwiML executed like
Twilio, REST pushes received on a virtual phone); the recruiter side is a
real browser on the backoffice. Both talk to the same live server.

Run:  python -m pytest -m e2e tests/e2e/channels/test_smoke.py   (add --headed to watch the recruiter)
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.support.candidates import unique_dni, unique_phone, unique_surname


ANSWERS = ["sí", None, "sí, tengo 34 años", "sí, tengo NIE con permiso", "mi DNI es {dni}", "Getafe",
           "sí, sin problema", "no, no lo tengo", "3 años de peón", "tengo coche", "el lunes"]


def script(name: str) -> list[str]:
    """One candidate: their own name and their own ID document (a shared one would make them one person)."""
    dni = unique_dni()
    return [name if a is None else a.format(dni=dni) for a in ANSWERS]


def test_recruiter_sees_one_candidate_across_whatsapp_and_phone(page: Page, channels, sim):
    name = f"Carlos {unique_surname()}"
    answers = script(name)
    phone = sim.phone(unique_phone(), name)

    phone.whatsapp("hola, vi la oferta de peón")
    for text in answers[:5]:  # up to the ID number on WhatsApp
        phone.whatsapp(text)
    call = phone.call()  # ... then they call from the same number
    assert "de nuevo" in call.prompt.lower() and "ciudad" in call.prompt.lower()
    for text in answers[5:8]:
        call.say(text)
    call.hangup()
    assert "oficio" in phone.whatsapp("sigo por aquí mejor")[0].lower()  # ... and finish on WhatsApp
    for text in answers[8:]:
        phone.whatsapp(text)

    [row] = channels.sessions_for(phone)
    assert channels.wait_for_status(row["id"])["status"] == "qualified"

    page.goto(f"{channels.url}/backoffice#/candidates/{row['id']}?tab=conversation")
    expect(page.get_by_test_id("candidate-name")).to_have_text(name)
    expect(page.locator(".cand-sub")).to_contain_text("WhatsApp → Teléfono")
    transcript = page.get_by_test_id("transcript")
    expect(transcript).to_contain_text("Getafe")  # said on the phone
    expect(transcript).to_contain_text("tengo coche")  # written on WhatsApp
    page.get_by_test_id("tab-activity").click()
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("El candidato cambió de canal: WhatsApp → Teléfono")
    expect(timeline).to_contain_text("El candidato cambió de canal: Teléfono → WhatsApp")


def test_recruiter_talks_to_a_whatsapp_candidate_from_the_backoffice(page: Page, channels, sim):
    name = f"Lucía {unique_surname()}"
    phone = sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    for text in script(name)[:3]:
        phone.whatsapp(text)
    [row] = channels.sessions_for(phone)

    page.goto(f"{channels.url}/backoffice#/candidates/{row['id']}?tab=conversation")
    expect(page.get_by_test_id("transcript")).to_contain_text("tengo 34 años")
    expect(page.get_by_text("Se envía por WhatsApp.")).to_be_visible()
    page.get_by_label("Mensaje al candidato").fill("Hola, soy Ana de RRHH. ¿Te viene bien una llamada mañana?")
    page.get_by_test_id("send-message").click()

    pushed = phone.wait_for_whatsapp("soy Ana de RRHH")  # through the real Twilio SDK -> simulator REST API
    assert pushed.via == "rest"
    assert phone.whatsapp("Sí, por la tarde mejor") == []  # the assistant stays quiet during the takeover
    expect(page.get_by_test_id("msg-cand").last).to_have_text(re.compile("por la tarde mejor"))

    mark = len(phone.thread)
    page.get_by_role("button", name="Devolver al asistente").click()
    handback = phone.wait_for_whatsapp("permiso", since=mark)
    assert "Sigo yo" in handback.body or "Gracias por esperar" in handback.body
    assert "DNI, NIE o pasaporte" in phone.whatsapp("sí, tengo permiso")[0]


def test_referral_registered_in_the_backoffice_continues_on_whatsapp(page: Page, channels, sim):
    name = f"Jorge {unique_surname()}"
    number = unique_phone()
    spaced = f"{number[:3]} {number[3:6]} {number[6:9]} {number[9:]}"
    page.goto(f"{channels.url}/backoffice#/pipeline")
    page.get_by_role("button", name="Nuevo candidato").click()
    dialog = page.get_by_role("dialog", name="Nuevo candidato")
    dialog.get_by_label("Proceso de selección").select_option("construction_laborer")
    dialog.get_by_label("Nombre y apellidos").fill(name)
    dialog.get_by_label("Contacto (teléfono o email)").fill(spaced)
    dialog.get_by_role("button", name="Crear candidato").click()
    link = dialog.get_by_test_id("screening-link").input_value()
    candidate_id = link.rsplit("session=", 1)[1]

    # instead of opening the web link, the candidate writes to the WhatsApp number

    opening = sim.phone(number).whatsapp("hola, me ha dado vuestro número el encargado")
    assert "Constructora Horizonte" in opening[0]
    [row] = channels.sessions_for(number)
    assert row["id"] == candidate_id  # the same candidate, now on WhatsApp

    dialog.get_by_role("link", name="Ver ficha del candidato").click()
    expect(page.get_by_test_id("candidate-name")).to_have_text(name)
    expect(page.locator(".cand-sub")).to_contain_text("Alta manual → WhatsApp")
    expect(page.locator(".cand-sub")).to_contain_text(number)  # stored the way channels address it


# ---------- WhatsApp through Meta's Cloud API, voice notes, the conversation registry ----------

def test_meta_candidate_with_voice_notes_and_a_call_in_the_backoffice(page: Page, channels_meta):
    from simulators.core import Faults

    sim = channels_meta.sim
    sim.faults = Faults()
    name = f"Rosa {unique_surname()}"
    phone = sim.phone(unique_phone(), name)
    assert "Constructora Horizonte" in phone.whatsapp("hola")[0]  # signed JSON webhook -> Graph API reply
    phone.whatsapp("sí")
    assert "18" in phone.voice_note(name)[0]  # the name, said in a voice note -> age question
    assert "permiso" in phone.voice_note("sí, tengo 29 años")[0]
    phone.photo()  # declined, never stored
    call = phone.call()
    call.say("sí, tengo permiso de trabajo", confidence=0.88)
    call.hangup()
    [row] = channels_meta.sessions_for(phone)

    page.goto(f"{channels_meta.url}/backoffice#/candidates/{row['id']}?tab=conversation")
    expect(page.get_by_test_id("candidate-name")).to_have_text(name)
    expect(page.locator(".cand-sub")).to_contain_text("WhatsApp → Teléfono")
    transcript = page.get_by_test_id("transcript")
    expect(transcript.get_by_test_id("channel-divider")).to_have_text(["WhatsApp", "Teléfono"])
    voice_note = transcript.locator(".msg.kind-voice_note").last
    expect(voice_note).to_contain_text("sí, tengo 29 años")
    expect(voice_note.get_by_test_id("msg-kind")).to_contain_text("nota de voz transcrita")
    expect(transcript.locator(".msg.kind-media")).to_contain_text("[Foto]")
    expect(transcript.locator(".msg.kind-speech")).to_contain_text("confianza 88%")

    # the recruiter writes: delivered through the Graph API, tracked until read
    page.get_by_label("Mensaje al candidato").fill("Rosa, ¿te viene bien empezar el lunes?")
    page.get_by_test_id("send-message").click()
    assert phone.wait_for_whatsapp("empezar el lunes").via == "graph"
