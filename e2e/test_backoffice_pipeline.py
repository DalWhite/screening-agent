"""Objective 2.1 — manual pipeline management and manual interaction.

Recruiters move candidates across the Kanban (drag & drop or the card menu),
schedule interviews, create candidates by hand, and talk to candidates
directly: taking over the conversation from the assistant, messaging the
candidate, handing back and reopening screenings.
"""

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from .conftest import (
    construction_physical, construction_qualified, construction_underage, courier_no_license, say, unique_surname,
)

pytestmark = pytest.mark.e2e


def open_board(page: Page, search: str) -> None:
    page.goto(f"/backoffice#/pipeline?q={quote(search)}")
    expect(page.get_by_label("Buscar en el pipeline")).to_have_value(search)


def card(page: Page, name: str):
    return page.get_by_test_id("candidate-card").filter(has_text=name)


def column(page: Page, stage: str):
    return page.get_by_test_id(f"column-{stage}")


def test_board_places_candidates_by_screening_outcome(page: Page, seed):
    tag = unique_surname()
    s = seed(ok=(construction_qualified, f"Carlos {tag}", "construction_laborer"),
             review=(construction_physical, f"Rosa {tag}", "construction_laborer"),
             ko=(construction_underage, f"Lucía {tag}", "construction_laborer"))
    open_board(page, tag)
    expect(column(page, "qualified").get_by_test_id("candidate-card")).to_have_count(1)
    expect(card(page, s["ok"][1])).to_be_visible()
    expect(column(page, "qualified")).to_contain_text(s["ok"][1])
    expect(column(page, "review")).to_contain_text(s["review"][1])      # flagged: physical limitations
    expect(column(page, "review")).to_contain_text("Revisar capacidad física")
    expect(column(page, "rejected")).to_contain_text(s["ko"][1])        # knockout: under 18
    expect(column(page, "qualified").get_by_test_id("column-count")).to_have_text("1")


def test_drag_and_drop_moves_a_candidate(page: Page, seed, api):
    s = seed(ok=(construction_qualified, "Carlos", "construction_laborer"))
    sid, name = s["ok"]
    open_board(page, name)
    expect(column(page, "qualified")).to_contain_text(name)

    card(page, name).drag_to(column(page, "shortlisted"))

    expect(column(page, "shortlisted")).to_contain_text(name)
    expect(column(page, "qualified")).not_to_contain_text(name)
    expect(page.locator(".toast").last).to_contain_text("Movido a «Preseleccionado»")
    page.reload()
    expect(column(page, "shortlisted")).to_contain_text(name)
    assert api.get(f"/api/bo/candidates/{sid}").json()["pipeline_stage"] == "shortlisted"


def test_move_with_the_card_menu(page: Page, seed):
    s = seed(ok=(construction_qualified, "Marcos", "construction_laborer"))
    name = s["ok"][1]
    open_board(page, name)
    card(page, name).get_by_role("button", name=f"Acciones de {name}").click()
    page.get_by_test_id("move-offer").click()
    expect(column(page, "offer")).to_contain_text(name)


def test_card_opens_the_quick_view_to_triage_in_place(page: Page, seed, api):
    s = seed(ok=(construction_qualified, "Nuria", "construction_laborer"))
    sid, name = s["ok"]
    open_board(page, name)
    card(page, name).click()
    panel = page.get_by_test_id("quick-view")
    expect(panel.get_by_role("heading", name=name)).to_be_visible()
    expect(panel).to_contain_text("Documento de identidad")
    expect(panel).to_contain_text("DNI •••••678Z")
    panel.get_by_test_id("advance").click()  # one click: next stage
    expect(panel.get_by_test_id("stage-pill")).to_contain_text("Preseleccionado")
    expect(column(page, "shortlisted")).to_contain_text(name)  # the board behind refreshes
    panel.get_by_label("Nota rápida").fill("Llamar el lunes")
    panel.get_by_label("Nota rápida").press("Enter")
    expect(panel).to_contain_text("Llamar el lunes")
    page.keyboard.press("Escape")
    expect(panel).to_have_count(0)
    assert api.get(f"/api/bo/candidates/{sid}").json()["notes_recruiter"][0]["body"] == "Llamar el lunes"


def test_stages_are_edited_in_place(page: Page, seed, api):
    s = seed(ok=(construction_qualified, "Teo", "construction_laborer"))
    sid, name = s["ok"]
    open_board(page, name)
    # add a stage right after "Pre-calificado"
    page.get_by_role("button", name="Opciones de la etapa Pre-calificado").click()
    page.get_by_role("menuitem", name="Añadir etapa después").click()
    new_name = f"Llamada {unique_surname()}"
    page.get_by_label("Nombre de la nueva etapa").fill(new_name)
    page.get_by_label("Nombre de la nueva etapa").press("Enter")
    expect(page.locator(".column").nth(3)).to_contain_text(new_name)
    stage_id = next(st["id"] for st in api.get("/api/bo/stages").json()["stages"] if st["label"] == new_name)

    # rename by double-clicking the title
    page.get_by_test_id(f"column-{stage_id}").locator("h2").dblclick()
    page.get_by_label(f"Nombre de la etapa {new_name}").fill(f"{new_name} RRHH")
    page.get_by_label(f"Nombre de la etapa {new_name}").press("Enter")
    expect(page.get_by_test_id(f"column-{stage_id}")).to_contain_text(f"{new_name} RRHH")

    # colour and order
    page.get_by_role("button", name=f"Opciones de la etapa {new_name} RRHH").click()
    page.get_by_role("menuitem", name="Cambiar color").click()
    page.get_by_role("button", name="Color pink").click()
    expect(page.get_by_test_id(f"column-{stage_id}").locator("header .dot")).to_have_class(re.compile("tone-pink"))
    page.get_by_role("button", name=f"Opciones de la etapa {new_name} RRHH").click()
    page.get_by_role("menuitem", name="Mover a la izquierda").click()
    expect(page.locator(".column").nth(2)).to_contain_text(f"{new_name} RRHH")

    # system stages cannot be deleted
    page.get_by_role("button", name="Opciones de la etapa Descartado").click()
    expect(page.get_by_role("menuitem", name="Etapa del sistema (no se puede eliminar)")).to_be_disabled()
    page.keyboard.press("Escape")

    # a stage with candidates asks where to move them
    card(page, name).drag_to(page.get_by_test_id(f"column-{stage_id}"))
    expect(page.get_by_test_id(f"column-{stage_id}")).to_contain_text(name)
    page.get_by_role("button", name=f"Opciones de la etapa {new_name} RRHH").click()
    page.get_by_role("menuitem", name="Eliminar etapa").click()
    dialog = page.get_by_role("dialog", name=f"Eliminar «{new_name} RRHH»")
    dialog.get_by_label("Mover sus candidatos a").select_option("interview")
    dialog.get_by_role("button", name="Eliminar y mover").click()
    expect(page.get_by_test_id(f"column-{stage_id}")).to_have_count(0)
    expect(column(page, "interview")).to_contain_text(name)
    assert api.get(f"/api/bo/candidates/{sid}").json()["pipeline_stage"] == "interview"


def test_schedule_interview_from_the_candidate_file(page: Page, seed):
    s = seed(ok=(construction_qualified, "Pablo", "construction_laborer"))
    sid, name = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    page.get_by_role("button", name="Programar entrevista").first.click()
    page.get_by_label("Fecha y hora de la entrevista").fill("2030-01-15T10:30")
    page.get_by_role("button", name="Guardar y mover a Entrevista").click()
    expect(page.locator(".cand-actions").get_by_test_id("stage-pill")).to_contain_text("Entrevista")
    expect(page.get_by_test_id("interview-chip")).to_contain_text("15 ene 2030")
    page.get_by_test_id("tab-activity").click()
    expect(page.get_by_test_id("timeline")).to_contain_text("Ana Ruiz movió el candidato a «Entrevista»")


def test_recruiter_takes_over_the_conversation(page: Page, candidate_page: Page, api):
    # the candidate is half-way through the construction screening in their own browser
    name = f"Marta {unique_surname()}"
    candidate_page.goto("/?job=construction_laborer")
    for text in ["hola", name, "sí"]:
        say(candidate_page, text)
    sid = api.get("/api/bo/candidates", params={"q": name}).json()["items"][0]["id"]

    # recruiter opens the conversation and writes: the assistant pauses
    page.goto(f"/backoffice#/candidates/{sid}?tab=conversation")
    expect(page.get_by_test_id("takeover-banner")).to_contain_text("Andrés (asistente) atiende esta conversación")
    page.get_by_label("Mensaje al candidato").fill("Hola, soy Ana de RRHH. ¿Puedes hablar mañana a las 10?")
    page.get_by_test_id("send-message").click()
    expect(page.get_by_test_id("takeover-banner")).to_contain_text("Estás atendiendo esta conversación")
    expect(page.get_by_test_id("msg-recruiter").last).to_contain_text("¿Puedes hablar mañana a las 10?")

    # the candidate receives it live in the chat
    recruiter_bubble = candidate_page.locator(".bubble.recruiter")
    expect(recruiter_bubble).to_contain_text("¿Puedes hablar mañana a las 10?")
    expect(recruiter_bubble).to_contain_text("Ana Ruiz")
    expect(candidate_page.locator("#stage-chip")).to_have_text("Hablando con el equipo de selección")

    # the candidate answers: the assistant stays silent, the recruiter sees the reply
    candidate_page.get_by_label("Tu respuesta").fill("Sí, a las 10 me va bien")
    candidate_page.get_by_role("button", name="Enviar").click()
    expect(candidate_page.locator(".bubble.system")).to_contain_text("te responderá aquí")
    expect(page.get_by_test_id("msg-cand").last).to_have_text(re.compile("Sí, a las 10 me va bien"))

    # hand back: the assistant re-asks the pending question in the candidate's chat
    page.get_by_role("button", name="Devolver al asistente").click()
    expect(page.get_by_test_id("takeover-banner")).to_contain_text("atiende esta conversación")
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("Sigo yo con las preguntas")
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("permiso para trabajar")
    candidate_page.get_by_label("Tu respuesta").fill("sí")
    candidate_page.get_by_role("button", name="Enviar").click()
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("DNI, NIE o pasaporte")

    page.get_by_test_id("tab-activity").click()
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("Ana Ruiz tomó el control de la conversación")
    expect(timeline).to_contain_text("Ana Ruiz envió un mensaje al candidato")
    expect(timeline).to_contain_text("Ana Ruiz devolvió la conversación al asistente")


def test_reopen_a_rejected_candidate(page: Page, candidate_page: Page, seed):
    s = seed(ko=(courier_no_license, "Pedro", None))
    sid, name = s["ko"]
    candidate_page.goto(f"/?session={sid}")
    expect(candidate_page.locator(".outcome .tag")).to_have_text("No califica para el puesto")

    page.goto(f"/backoffice#/candidates/{sid}?tab=conversation")
    page.get_by_test_id("reopen").click()
    expect(page.locator(".toast").last).to_contain_text("Screening reabierto")
    expect(page.get_by_test_id("msg-bot").last).to_contain_text("licencia de conducir")

    # the candidate sees the question again and can continue
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("licencia de conducir")
    expect(candidate_page.get_by_label("Tu respuesta")).to_be_enabled()
    candidate_page.get_by_label("Tu respuesta").fill("perdón, sí que tengo carnet")
    candidate_page.get_by_role("button", name="Enviar").click()
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("ciudad")


def test_create_a_candidate_by_hand_and_send_the_screening_link(page: Page, candidate_page: Page):
    name = f"Jorge {unique_surname()}"
    page.goto("/backoffice#/pipeline")
    page.get_by_role("button", name="Nuevo candidato").click()
    dialog = page.get_by_role("dialog", name="Nuevo candidato")
    dialog.get_by_label("Proceso de selección").select_option("construction_laborer")
    dialog.get_by_label("Nombre y apellidos").fill(name)
    dialog.get_by_label("Contacto (teléfono o email)").fill("+34 600 123 123")
    dialog.get_by_label("Nota interna (opcional)").fill("Referido por el encargado de Getafe")
    dialog.get_by_role("button", name="Crear candidato").click()
    link = dialog.get_by_test_id("screening-link").input_value()
    assert re.search(r"/\?session=[0-9a-f-]{36}$", link)

    # the candidate opens the link: the name is known, so the assistant skips that question
    candidate_page.goto(link)
    expect(candidate_page.locator("#brand")).to_contain_text("Constructora Horizonte")
    candidate_page.get_by_label("Tu respuesta").fill("hola")
    candidate_page.get_by_role("button", name="Enviar").click()
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("mayor de 18")

    dialog.get_by_role("link", name="Ver ficha del candidato").click()
    expect(page.get_by_test_id("candidate-name")).to_have_text(name)
    expect(page.get_by_test_id("notes")).to_contain_text("Referido por el encargado de Getafe")
    expect(page.locator(".cand-sub")).to_contain_text("Alta manual")


def test_busy_columns_scroll_and_link_to_the_full_list(page: Page, api):
    """With volume, each column scrolls on its own and shows the latest 50 cards; the rest is one click away."""
    tag = unique_surname()
    for i in range(55):
        r = api.post("/api/bo/candidates", json={"profile_id": "construction_laborer", "name": f"Lote{i} {tag}"})
        assert r.status_code in (200, 201), r.text
    open_board(page, tag)
    col = column(page, "screening")
    expect(col.get_by_test_id("column-count")).to_have_text("55")
    expect(col.get_by_test_id("candidate-card")).to_have_count(50)
    assert col.locator(".col-list").evaluate("el => el.scrollHeight > el.clientHeight")  # scrolls inside the column
    more = page.get_by_test_id("column-more-screening")
    expect(more).to_have_text(re.compile("Ver los 55 en la lista"))
    expect(page.get_by_test_id("column-more-rejected")).to_have_count(0)  # short columns have no link
    more.click()
    expect(page).to_have_url(re.compile(r"#/candidates\?.*pipeline_stage=screening"))
    expect(page.get_by_test_id("search")).to_have_value(tag)
    expect(page.get_by_text("55 candidatos")).to_be_visible()
