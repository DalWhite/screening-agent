"""Objective 1 — recruiting processes are data that recruiters manage.

Basic mode speaks recruiter language (questions as a list, rules as sentences,
scoring as "Sumar N puntos si…"); "Configuración avanzada" reveals the
technical layer. Drafts autosave, problems point at the offending item, a
publication is a new revision. Editing tests work on private copies.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import VALID_ID, copy_profile, run_chat, say, unique_surname

pytestmark = pytest.mark.e2e


def open_editor(page: Page, pid: str, section: str) -> None:
    page.goto(f"/backoffice#/processes/{pid}?tab={section}")
    expect(page.get_by_test_id(f"ptab-{section}")).to_have_attribute("aria-selected", "true")


def expect_saved(page: Page) -> None:
    expect(page.get_by_test_id("save-state")).to_have_text("Borrador guardado")
    expect(page.get_by_test_id("draft-chip")).to_be_visible()


def publish(page: Page, note: str = "") -> None:
    page.get_by_test_id("publish").click()
    dialog = page.get_by_role("dialog", name="Publicar cambios")
    dialog.get_by_label("Nota de la publicación").fill(note)
    dialog.get_by_test_id("confirm-publish").click()
    expect(page.locator(".toast").last).to_contain_text("Publicado: revisión")


def commit(locator) -> None:
    locator.press("Tab")


def test_process_list_is_clean_with_one_menu_of_actions(page: Page, api):
    run_chat(api, ["hola"], "construction_laborer")  # a process with candidates can never be deleted
    page.goto("/backoffice#/processes")
    row = page.get_by_test_id("process-construction_laborer")
    expect(row).to_contain_text("Peón / ayudante de obra")
    expect(row).to_contain_text("Activo")
    expect(page.get_by_text("Ver candidatos")).to_have_count(0)  # removed: redundant with the process selector
    row.get_by_role("button", name="Más acciones de Peón / ayudante de obra").click()
    for action in ("Duplicar", "Desactivar", "Abrir chat de candidato"):
        expect(page.get_by_role("menuitem", name=action)).to_be_visible()
    expect(page.get_by_role("menuitem", name="Eliminar (tiene candidatos)")).to_be_disabled()
    page.keyboard.press("Escape")
    row.get_by_role("link", name="Editar").click()
    expect(page).to_have_url(re.compile(r"#/processes/construction_laborer"))
    expect(page.get_by_test_id("process-title")).to_have_text("Peón / ayudante de obra")


def test_basic_mode_hides_technical_settings_until_advanced_is_on(page: Page, api):
    pid = copy_profile(api, "construction_laborer")
    open_editor(page, pid, "questions")
    expect(page.get_by_test_id("ptab-checks")).to_have_count(0)
    expect(page.get_by_label("Texto de la pregunta Mayor de edad")).to_have_value("¿Eres mayor de 18 años?")
    expect(page.get_by_text("dato is_adult")).to_have_count(0)
    page.get_by_role("button", name="Ver detalles de pregunta Transporte a obra").click()
    expect(page.get_by_label("Opción 3 de Transporte a obra")).to_have_value("Sin medio propio")
    expect(page.get_by_label("ID (etapa)")).to_have_count(0)

    page.get_by_text("Configuración avanzada").click()
    expect(page.get_by_test_id("ptab-checks")).to_be_visible()
    expect(page.get_by_text("id adult · dato is_adult")).to_be_visible()
    expect(page.get_by_label("ID (etapa)")).to_have_value("transport")
    expect(page.get_by_test_id("options-table-transport")).to_contain_text("Sinónimos que reconoce")
    page.get_by_test_id("ptab-checks").click()
    expect(page.get_by_test_id("table-checks")).to_contain_text("Registro de identidad (simulado)")

    page.get_by_text("Configuración avanzada").click()  # back to basic: hidden again
    expect(page.get_by_test_id("ptab-checks")).to_have_count(0)
    expect(page.get_by_test_id("ptab-questions")).to_have_attribute("aria-selected", "true")

    # content language switch
    page.get_by_role("radio", name="EN").click()
    expect(page.get_by_label("Texto de la pregunta Mayor de edad")).to_have_value("Are you 18 or older?")


def test_edit_a_question_and_publish_a_revision(page: Page, candidate_page: Page, api):
    pid = copy_profile(api, "construction_laborer")
    open_editor(page, pid, "questions")
    text = page.get_by_label("Texto de la pregunta Mayor de edad")
    text.fill("¿Tienes ya 18 años cumplidos?")
    commit(text)
    expect_saved(page)

    candidate_page.goto(f"/?job={pid}")  # drafts are not live yet
    say(candidate_page, "hola")
    say(candidate_page, f"Ana {unique_surname()}")
    expect(candidate_page.locator(".bubble.agent").last).to_have_text("¿Eres mayor de 18 años?")

    publish(page, "Pregunta de edad más clara")
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Publicado r2")
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Sin cambios pendientes")
    sid = run_chat(api, ["hola", f"Ana {unique_surname()}"], pid)
    assert api.get(f"/api/sessions/{sid}").json()["messages"][-1]["content"] == "¿Tienes ya 18 años cumplidos?"
    say(candidate_page, "sí")  # the candidate who started on r1 keeps r1
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("permiso para trabajar")

    page.get_by_test_id("ptab-history").click()
    versions = page.get_by_test_id("versions")
    expect(versions.locator("tbody tr")).to_have_count(2)
    expect(versions.locator("tbody tr").first).to_contain_text("Pregunta de edad más clara")


def test_a_rule_written_as_a_sentence_and_tried_before_publishing(page: Page, api):
    pid = copy_profile(api, "construction_laborer")
    open_editor(page, pid, "rules")
    page.get_by_test_id("add-rule").click()
    rule = page.get_by_test_id("item-rules").last
    rule.get_by_label(re.compile("^Dato de la regla")).select_option("transport")
    rule.get_by_label(re.compile("^Valor de la regla")).select_option("none")
    expect(rule.get_by_label(re.compile("^Efecto de la regla"))).to_have_value("disqualify")
    message = rule.get_by_label(re.compile("^Mensaje al candidato"))
    message.fill("Necesitamos que puedas llegar a la obra por tu cuenta. ¡Gracias!")
    commit(message)
    expect_saved(page)
    expect(page.get_by_test_id("problem-count")).to_have_count(0)

    page.get_by_test_id("open-simulator").click()
    box = page.get_by_role("textbox", name="Mensaje de prueba")
    for text in ["hola", "Ana Gil Mora", "sí", "sí", VALID_ID, "Toledo", "sí", "sí", "0", "voy andando"]:
        box.fill(text)
        box.press("Enter")
        expect(page.get_by_test_id("sim-output").locator(".msg.cand").last).to_have_text(text)
    expect(page.get_by_test_id("sim-status")).to_have_text("disqualified")
    expect(page.get_by_test_id("sim-output")).to_contain_text("Necesitamos que puedas llegar a la obra")
    page.get_by_role("button", name="Cerrar panel").click()

    publish(page)
    sid = run_chat(api, ["hola", "Ana Gil Mora", "sí", "sí", VALID_ID, "Toledo", "sí", "sí", "0", "voy andando"], pid)
    session = api.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "disqualified" and session["disqualify_reason"].startswith("transporte_a_obra")


def test_problems_point_at_the_item_and_block_publication(page: Page, api):
    pid = copy_profile(api, "construction_laborer")
    open_editor(page, pid, "questions")
    page.get_by_role("button", name="Opciones de pregunta Mayor de edad").click()
    page.get_by_test_id("delete-item").click()
    expect(page.get_by_test_id("problem-count")).to_have_text("1 problema")
    expect(page.get_by_test_id("problems")).to_contain_text("la regla «underage» usa un dato desconocido «is_adult»")
    expect(page.get_by_test_id("publish")).to_be_disabled()

    page.get_by_test_id("problems").get_by_role("button", name="ir →").click()
    expect(page.get_by_test_id("ptab-rules")).to_have_attribute("aria-selected", "true")
    expect(page.locator('[data-path="rules.0.field"]')).to_have_class(re.compile("has-problem"))

    page.get_by_role("button", name="Más acciones del proceso").click()
    page.get_by_role("menuitem", name="Descartar borrador").click()
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Sin cambios pendientes")
    expect(page.get_by_test_id("problem-count")).to_have_count(0)
    page.get_by_test_id("ptab-questions").click()
    expect(page.get_by_label("Nombre de la pregunta Mayor de edad")).to_have_value("Mayor de edad")


def test_scoring_and_general_settings(page: Page, api):
    pid = copy_profile(api, "construction_laborer")
    open_editor(page, pid, "scoring")
    expect(page.get_by_test_id("score-total")).to_contain_text("Máximo alcanzable: 100 puntos")
    points = page.get_by_label("Puntos del criterio Curso PRL")
    points.fill("40")
    commit(points)
    expect(page.get_by_test_id("score-total")).to_contain_text("Máximo alcanzable: 120 puntos")
    expect_saved(page)
    page.get_by_test_id("ptab-general").click()
    company = page.get_by_label("Empresa")
    company.fill("Horizonte Obras SL")
    commit(company)
    expect_saved(page)
    publish(page)
    detail = api.get(f"/api/bo/profiles/{pid}").json()["published"]
    assert detail["company"] == "Horizonte Obras SL" and detail["scoring"][0]["points"] == 40


def test_create_a_process_from_scratch_and_screen_with_it(page: Page, candidate_page: Page):
    role = f"Carretillero {unique_surname()}"
    page.goto("/backoffice#/processes")
    page.get_by_role("button", name="Nuevo proceso").click()
    dialog = page.get_by_role("dialog", name="Nuevo proceso")
    dialog.get_by_label("Puesto").fill(role)
    dialog.get_by_label("Empresa").fill("Almacenes Norte")
    pid = dialog.get_by_label("Identificador (no se puede cambiar)").input_value()
    assert pid == role.lower().replace(" ", "_")  # suggested from the role
    dialog.get_by_role("button", name="Crear borrador").click()
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Sin publicar")

    page.get_by_test_id("ptab-questions").click()
    page.get_by_test_id("add-question").click()
    name = page.get_by_label("Nombre de la pregunta Nueva pregunta")
    name.fill("Carné de carretilla")
    commit(name)
    page.get_by_label("Tipo de respuesta de Carné de carretilla").select_option("yes_no")
    text = page.get_by_label("Texto de la pregunta Carné de carretilla")
    text.fill("¿Tienes el carné de carretillero?")
    commit(text)
    page.get_by_role("button", name="Opciones de pregunta Carné de carretilla").click()
    page.get_by_role("menuitem", name="Mover arriba").click()  # before the start date
    expect(page.get_by_test_id("item-questions").nth(1)).to_have_attribute("aria-label", "pregunta Carné de carretilla")

    page.get_by_test_id("ptab-rules").click()
    page.get_by_test_id("add-rule").click()
    rule = page.get_by_test_id("item-rules").last
    expect(rule.get_by_label(re.compile("^Dato de la regla"))).to_have_value("carne_de_carretilla")
    rule.get_by_label(re.compile("^Valor de la regla")).select_option("false")
    message = rule.get_by_label(re.compile("^Mensaje al candidato"))
    message.fill("Para este puesto es imprescindible el carné de carretillero.")
    commit(message)
    expect_saved(page)
    publish(page, "Primera versión")
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Publicado r1")

    candidate_page.goto("/")
    candidate_page.locator(f'.job[data-job="{pid}"]').click()
    expect(candidate_page.locator("#brand")).to_contain_text("Almacenes Norte")
    say(candidate_page, "hola")
    say(candidate_page, f"Luis {unique_surname()}")
    expect(candidate_page.locator(".bubble.agent").last).to_have_text("¿Tienes el carné de carretillero?")
    say(candidate_page, "no")
    expect(candidate_page.locator(".bubble.agent").last).to_have_text("Para este puesto es imprescindible el carné de carretillero.")
    expect(candidate_page.locator(".outcome .tag")).to_have_text("No califica para el puesto")


def test_duplicate_then_delete_an_unused_process(page: Page, api):
    run_chat(api, ["hola"], "delivery_courier")
    page.goto("/backoffice#/processes")
    page.get_by_role("button", name="Más acciones de Repartidor/a (conductor)").click()
    page.get_by_role("menuitem", name="Duplicar").click()
    dialog = page.get_by_role("dialog", name=re.compile("Duplicar"))
    pid = f"reparto_{unique_surname().lower()}"
    dialog.get_by_label("Identificador (no se puede cambiar)").fill(pid)
    dialog.get_by_role("button", name="Crear borrador").click()
    expect(page.get_by_test_id("draft-bar")).to_contain_text("Sin publicar")
    expect(page.get_by_label("Nombre de la pregunta Licencia de conducir")).to_be_visible()  # everything was copied

    page.goto("/backoffice#/processes")
    row = page.get_by_test_id(f"process-{pid}")
    expect(row).to_contain_text("Borrador sin publicar")
    row.get_by_role("button", name=re.compile("^Más acciones de")).click()
    page.get_by_role("menuitem", name="Eliminar", exact=True).click()
    page.get_by_role("button", name="Eliminar definitivamente").click()
    expect(page.get_by_test_id(f"process-{pid}")).to_have_count(0)


def test_deactivate_hides_the_job_from_candidates(page: Page, candidate_page: Page, api):
    pid = copy_profile(api, "delivery_courier")
    page.goto("/backoffice#/processes")
    row = page.get_by_test_id(f"process-{pid}")
    row.get_by_role("button", name=re.compile("^Más acciones de")).click()
    page.get_by_role("menuitem", name="Desactivar").click()
    expect(page.get_by_test_id(f"process-{pid}")).to_contain_text("Inactivo")
    candidate_page.goto("/")
    expect(candidate_page.locator('.job[data-job="delivery_courier"]')).to_be_visible()
    expect(candidate_page.locator(f'.job[data-job="{pid}"]')).to_have_count(0)
    page.get_by_test_id(f"process-{pid}").get_by_role("button", name=re.compile("^Más acciones de")).click()
    page.get_by_role("menuitem", name="Activar").click()
    expect(page.get_by_test_id(f"process-{pid}")).to_contain_text("Activo")
    candidate_page.reload()
    expect(candidate_page.locator(f'.job[data-job="{pid}"]')).to_be_visible()


def test_restore_an_old_revision_into_the_draft(page: Page, api):
    pid = copy_profile(api, "delivery_courier")
    open_editor(page, pid, "general")
    company = page.get_by_label("Empresa")
    company.fill("Sazón Express")
    commit(company)
    expect_saved(page)
    publish(page, "Nueva marca")
    page.get_by_test_id("ptab-history").click()
    page.get_by_test_id("versions").locator("tbody tr").nth(1).get_by_role("button", name="Restaurar al borrador").click()
    expect(page.get_by_test_id("draft-chip")).to_be_visible()
    page.get_by_test_id("ptab-general").click()
    expect(page.get_by_label("Empresa")).to_have_value("Grupo Sazón")
