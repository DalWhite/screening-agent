"""The channel simulator's own UI (python -m simulators --with-app): what a tester uses by hand.

The UI drives the candidate side (phones, WhatsApp, calls, faults); these tests
check it tells the truth about what the app does: replies, pushes, failures,
templates, signatures and call flow.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from tests.support.candidates import unique_phone
from tests.support.channels import CONSTRUCTION, only_session


def add_phone(page: Page, stack, name: str = "Marta") -> str:
    number = unique_phone()
    page.goto(stack.sim_server.url + "/")
    page.get_by_label("Número de teléfono").fill(number)
    page.get_by_label("Nombre del contacto").fill(name)
    page.get_by_role("button", name="Añadir").click()
    expect(page.locator("#phone-list li.selected")).to_contain_text(name)
    return number


def whatsapp(page: Page, text: str) -> None:
    before = page.locator(".msg.business").count()
    page.get_by_label("Mensaje de WhatsApp").fill(text)
    page.get_by_role("button", name="Enviar").click()
    expect(page.locator(".msg.business")).not_to_have_count(before)


def test_phone_simulator_ui(page: Page, channels, sim):
    """The simulator's own UI: what a tester uses by hand (python -m simulators --with-app)."""
    number = unique_phone()
    page.goto(channels.sim_server.url + "/")
    page.get_by_label("Número de teléfono").fill(number)
    page.get_by_label("Nombre del contacto").fill("Marta")
    page.get_by_role("button", name="Añadir").click()
    expect(page.locator("#phone-list li.selected")).to_contain_text("Marta")

    def whatsapp(text: str) -> None:
        before = page.locator(".msg.business").count()
        page.get_by_label("Mensaje de WhatsApp").fill(text)
        page.get_by_role("button", name="Enviar").click()
        expect(page.locator(".msg.business")).to_have_count(before + 1)

    whatsapp("hola")
    expect(page.locator(".msg.business").last).to_contain_text("Constructora Horizonte")
    whatsapp("sí")
    whatsapp("Marta López Gil")
    expect(page.locator(".msg.business").last).to_contain_text("18")

    page.get_by_role("button", name="📞 Llamar").click()
    expect(page.locator("#call-status")).to_contain_text("Twilio escucha (es-ES)")
    expect(page.locator(".line.agent").last).to_contain_text("18")  # same screening, same pending question
    page.get_by_label("Lo que dices").fill("sí, tengo 29 años")
    page.get_by_role("button", name="Hablar").click()
    expect(page.locator(".line.agent").last).to_contain_text("permiso para trabajar")
    page.get_by_role("button", name="Colgar").click()
    expect(page.locator("#call-status")).to_contain_text("colgó el candidato")

    log = page.get_by_role("complementary", name="Tráfico de webhooks y APIs")
    expect(log).to_contain_text("/api/channels/voice/status")  # the status callback Twilio sends
    page.get_by_role("button", name="Cerrar ventana 24 h").click()
    expect(page.locator("#wa-window")).to_have_text("ventana 24 h cerrada")


def test_voice_note_from_the_phone_simulator_ui(page: Page, channels_meta):
    page.goto(channels_meta.sim_server.url + "/")
    page.get_by_label("Número de teléfono").fill(unique_phone())
    page.get_by_role("button", name="Añadir").click()
    expect(page.locator("#config")).to_contain_text("vía Meta Cloud API")
    page.get_by_label("Mensaje de WhatsApp").fill("hola")
    page.get_by_role("button", name="Enviar").click()
    expect(page.locator(".msg.business").last).to_contain_text("Constructora Horizonte")

    page.get_by_label("Mensaje de WhatsApp").fill("sí, empecemos")
    page.get_by_title("Enviar como nota de voz lo que has escrito").click()
    expect(page.locator(".msg.candidate audio")).to_have_count(1)  # playable simulated audio
    expect(page.locator(".msg.candidate").last).to_contain_text("🎤 “sí, empecemos”")
    expect(page.locator(".msg.business").last).to_contain_text("Nombre y apellido")  # understood: next question
    expect(page.locator(".msg.candidate .ticks.read").first).to_be_visible()  # the app sent read receipts


def test_fault_toggles_change_what_the_app_experiences(page: Page, live):
    number = add_phone(page, live, "Carmen")
    whatsapp(page, "hola")
    whatsapp(page, "sí")
    session_id = only_session(live, number)["id"]

    page.get_by_label("API caída (Twilio 503 / Meta código 2)").check()
    deadline = time.time() + 5
    while not live.sim.faults.rest_down and time.time() < deadline:
        page.wait_for_timeout(50)  # the toggle is a POST to the simulator
    assert live.sim.faults.rest_down
    sent = live.api.post(f"/api/bo/candidates/{session_id}/messages",
                         json={"text": "Carmen, ¿puedes hablar ahora?", "pause_bot": False}).json()
    assert sent["delivered"] is False
    log = page.get_by_role("complementary", name="Tráfico de webhooks y APIs")
    expect(log.locator(".err").first).to_be_visible()  # the refused API call, in red

    page.get_by_label("API caída (Twilio 503 / Meta código 2)").uncheck()
    whatsapp(page, "perdona, ¿me decías?")
    expect(page.locator(".msg.business", has_text="Carmen, ¿puedes hablar ahora?")).to_be_visible()


def test_a_failed_delivery_and_its_template_are_shown(page: Page, channels_meta):
    stack = channels_meta
    number = add_phone(page, stack, "Sofía")
    whatsapp(page, "hola")
    whatsapp(page, "sí")
    whatsapp(page, "Sofía Ruiz Gil")
    page.get_by_role("button", name="Cerrar ventana 24 h").click()
    expect(page.locator("#wa-window")).to_have_text("ventana 24 h cerrada")
    session_id = only_session(stack, number)["id"]
    stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "¿Sigues interesada?", "pause_bot": False})

    failed = page.locator(".msg.business.failed", has_text="¿Sigues interesada?")
    expect(failed).to_contain_text("no entregado (error 131047)", timeout=15_000)
    expect(page.locator(".msg.business[data-via='template']").last).to_contain_text("Hola Sofía")


def test_meta_webhook_verification_button(page: Page, channels_meta):
    add_phone(page, channels_meta)
    page.get_by_role("button", name="Verificar webhook (Meta)").click()
    expect(page.locator("#toast")).to_contain_text("Webhook verificado")


def test_rejected_webhooks_show_up_in_the_traffic_log(page: Page, stacks):
    stack = stacks.get("twilio", PUBLIC_BASE_URL="https://old-tunnel.example", **CONSTRUCTION)
    add_phone(page, stack, "Diego")
    page.get_by_label("Mensaje de WhatsApp").fill("hola")
    page.get_by_role("button", name="Enviar").click()
    log = page.get_by_role("complementary", name="Tráfico de webhooks y APIs")
    expect(log).to_contain_text("11200: HTTP retrieval failure (status 403)")
    expect(page.locator(".msg.business")).to_have_count(0)  # nothing answers: the signature was rejected


def test_a_call_driven_from_the_ui(page: Page, live_voice):
    add_phone(page, live_voice, "Víctor")
    page.get_by_role("button", name="📞 Llamar").click()
    provider = {"twilio": "Twilio", "vonage": "Vonage"}[live_voice.sim.voice_provider]
    expect(page.locator("#call-status")).to_contain_text(f"{provider} escucha (es-ES)")
    page.get_by_role("button", name="Silencio").click()
    expect(page.locator(".line.system")).to_contain_text("(silencio)")
    expect(page.locator(".line.agent").last).to_contain_text("¿Sigues ahí?")
    page.get_by_label("Lo que dices").fill("sí, empecemos")
    page.get_by_role("button", name="Hablar").click()
    expect(page.locator(".line.caller").last).to_have_text("Sí, empecemos.")  # as Twilio's STT writes it
    expect(page.locator(".line.agent").last).to_contain_text("Nombre y apellido")
    page.get_by_role("button", name="Colgar").click()
    expect(page.locator("#call-status")).to_contain_text("colgó el candidato")
    page.get_by_role("button", name="📞 Llamar").click()  # call back: same screening
    expect(page.locator(".line.agent").last).to_contain_text("de nuevo")
    expect(page.locator("#past-calls")).to_be_visible()
