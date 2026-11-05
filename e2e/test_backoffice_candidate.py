"""Objectives 2.2 / 2.3 — candidate profile and screening results.

The candidate file shows what the assistant collected (editable by the
recruiter, with an audit trail), recruiter-owned data (owner, tags, notes),
and a screening report: answer per question, what the candidate literally
said, business rules evaluated and the ranking score breakdown.
"""

import pytest
from playwright.sync_api import Page, expect

from .conftest import construction_qualified, construction_underage, courier_qualified

pytestmark = pytest.mark.e2e


def test_profile_view_shows_collected_data(page: Page, seed):
    s = seed(ok=(construction_qualified, "Carlos", "construction_laborer"))
    sid, name = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    expect(page.get_by_test_id("candidate-name")).to_have_text(name)
    expect(page.locator(".cand-sub")).to_contain_text("Peón / ayudante de obra · Constructora Horizonte")
    expect(page.locator(".cand-sub")).to_contain_text("Pre-calificado")
    expect(page.get_by_test_id("value-location")).to_contain_text("Madrid (Getafe), ES")
    expect(page.get_by_test_id("value-experience")).to_contain_text("3 años (peón / ayudante, albañilería)")
    expect(page.get_by_test_id("value-transport")).to_have_text("Vehículo propio")
    expect(page.get_by_test_id("value-prl")).to_have_text("No")
    expect(page.locator(".cand-score").get_by_test_id("score")).to_have_text("75")


def test_recruiter_corrects_data_and_score_updates(page: Page, seed):
    s = seed(ok=(construction_qualified, "Sergio", "construction_laborer"))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    page.get_by_role("button", name="Editar Curso PRL 20h").click()
    dialog = page.get_by_role("dialog", name="Editar: Curso PRL 20h")
    dialog.get_by_label("Curso PRL 20h").select_option("true")
    dialog.get_by_role("button", name="Guardar").click()
    expect(page.get_by_test_id("value-prl")).to_have_text("Sí")
    expect(page.locator(".cand-score").get_by_test_id("score")).to_have_text("95")  # +20 for the PRL course
    page.get_by_test_id("tab-activity").click()
    expect(page.get_by_test_id("timeline")).to_contain_text("Ana Ruiz corrigió datos")
    expect(page.get_by_test_id("timeline")).to_contain_text("has_prl_course: false → true")


def test_owner_tags_and_notes_persist(page: Page, seed):
    s = seed(ok=(courier_qualified, "Laura", None))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    page.get_by_label("Responsable").fill("Luis Gómez")
    page.get_by_label("Responsable").press("Tab")
    expect(page.locator(".toast").last).to_contain_text("Responsable actualizado")
    page.get_by_label("Nueva etiqueta").fill("moto propia")
    page.get_by_label("Nueva etiqueta").press("Enter")
    expect(page.get_by_test_id("tags")).to_contain_text("#moto propia")
    page.get_by_label("Nueva nota").fill("Disponible también sábados. Llamar por la tarde.")
    page.get_by_role("button", name="Añadir nota").click()
    expect(page.get_by_test_id("notes")).to_contain_text("Llamar por la tarde")
    expect(page.get_by_test_id("notes")).to_contain_text("Ana Ruiz")

    page.reload()
    expect(page.get_by_label("Responsable")).to_have_value("Luis Gómez")
    expect(page.get_by_test_id("tags")).to_contain_text("#moto propia")
    expect(page.get_by_test_id("notes")).to_contain_text("Llamar por la tarde")

    page.get_by_role("button", name="Quitar etiqueta moto propia").click()
    expect(page.get_by_test_id("tags")).not_to_contain_text("moto propia")
    page.get_by_role("button", name="Borrar nota").click()
    expect(page.get_by_test_id("notes")).to_contain_text("Todavía no hay notas")


def test_screening_results_of_a_qualified_candidate(page: Page, seed):
    s = seed(ok=(construction_qualified, "Carlos", "construction_laborer"))
    sid, name = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=screening")
    expect(page.get_by_test_id("outcome-banner")).to_contain_text("Pre-calificado por el asistente")
    answers = page.get_by_test_id("answers-table")
    expect(answers.locator("tbody tr")).to_have_count(10)
    experience = answers.locator("tr[data-question='experience']")
    expect(experience).to_contain_text("3 años (peón / ayudante, albañilería)")
    expect(experience).to_contain_text("3 años de peón y algo de albañilería")  # the literal answer
    expect(answers.locator("tr[data-question='adult']")).to_contain_text("Respondida")

    rules = page.get_by_test_id("rules-table")
    expect(rules.locator("tr[data-rule='underage']")).to_contain_text("Mayor de edad = No")
    expect(rules.locator("tr[data-rule='underage']")).to_contain_text("Cumple")
    expect(rules.locator("tr[data-rule='needs_prl_training']")).to_contain_text("Activada")
    expect(rules.locator("tr[data-rule='physical_review']")).to_contain_text("Cumple")

    breakdown = page.get_by_test_id("score-breakdown")
    expect(breakdown).to_contain_text("Vehículo propio")
    expect(breakdown).to_contain_text("20/20")
    expect(page.locator(".flag-list")).to_contain_text("Necesita curso PRL")


def test_screening_results_of_a_knocked_out_candidate(page: Page, seed):
    s = seed(ko=(construction_underage, "Lucía", "construction_laborer"))
    sid, _ = s["ko"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=screening")
    expect(page.get_by_test_id("outcome-banner")).to_contain_text("No califica · Menor de 18 años")
    answers = page.get_by_test_id("answers-table")
    expect(answers.locator("tr[data-question='adult']")).to_contain_text("Eliminatoria")
    expect(answers.locator("tr[data-question='adult']")).to_contain_text("tengo 17 años")
    expect(answers.locator("tr[data-question='work_permit']")).to_contain_text("Sin respuesta")
    expect(page.get_by_test_id("rules-table").locator("tr[data-rule='underage']")).to_contain_text("Activada")


def test_activity_timeline_tells_the_story(page: Page, seed):
    s = seed(ok=(construction_qualified, "Iván", "construction_laborer"))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=activity")
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("Regla activada: Necesita curso PRL")
    expect(timeline).to_contain_text("Pasó automáticamente a «Pre-calificado»")
    expect(timeline).not_to_contain_text("Pregunta «name» → «adult»")
    page.get_by_label("Mostrar eventos técnicos del asistente").check()
    expect(timeline).to_contain_text("Pregunta «name» → «adult»")
