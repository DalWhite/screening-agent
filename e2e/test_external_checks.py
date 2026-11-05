"""Objective 1 (v2) — business rules validated by external, asynchronous systems.

The construction process verifies the candidate's ID document against an
identity registry (a mock that answers after ~1s; test numbers decide the
result). The conversation never waits for it: the decision arrives later and
the rules react. Recruiters see every check, decide manual ones and retry
failures. The full ID number is never shown or stored in clear.
"""

import pytest
from playwright.sync_api import Page, expect

from .conftest import (
    UNKNOWN_ID, VALID_ID, construction_id_error, construction_qualified, copy_profile, courier_qualified, say,
    unique_surname,
)

pytestmark = pytest.mark.e2e


def reach_id_question(page: Page, job: str = "construction_laborer") -> None:
    page.goto(f"/?job={job}")
    for text in ["hola", f"Ana {unique_surname()}", "sí", "sí"]:
        say(page, text)
    expect(page.locator(".bubble.agent").last).to_contain_text("DNI, NIE o pasaporte")


def test_wrong_control_letter_is_caught_locally(page: Page):
    reach_id_question(page)
    say(page, "12345678A")
    expect(page.locator(".bubble.agent").last).to_contain_text("La letra no coincide con el número")
    say(page, "X1234567")
    expect(page.locator(".bubble.agent").last).to_contain_text("Eso no parece un DNI, NIE o pasaporte")


def test_unknown_document_knocks_the_candidate_out_while_chatting(page: Page):
    reach_id_question(page)
    say(page, f"es el {UNKNOWN_ID}")
    say(page, "Getafe")  # the conversation goes on while the registry is consulted...
    # ...and the registry's answer arrives on its own
    knockout = page.locator(".bubble.agent").filter(has_text="No hemos podido verificar tu documento")
    expect(knockout).to_be_visible(timeout=15_000)
    expect(page.get_by_label("Tu respuesta")).to_be_disabled(timeout=15_000)


def test_blocking_manual_check_waits_for_the_recruiter(page: Page, candidate_page: Page, api):
    # a copy of the construction process whose ID check is done by a recruiter by hand
    def manual(data):
        data["checks"][0]["provider"] = "manual"

    pid = copy_profile(api, "construction_laborer", mutate=manual)
    name = f"Pablo {unique_surname()}"
    candidate_page.goto(f"/?job={pid}")
    for text in construction_qualified(name):
        say(candidate_page, text)
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("Estoy verificando tus datos")
    expect(candidate_page.locator(".outcome .tag")).to_have_text("Verificando tus datos…")
    expect(candidate_page.get_by_label("Tu respuesta")).to_be_disabled()

    sid = api.get("/api/bo/candidates", params={"q": name}).json()["items"][0]["id"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=screening")
    expect(page.get_by_test_id("outcome-banner")).to_contain_text("En verificación")
    row = page.get_by_test_id("checks-table").locator("tr[data-check='id_check']")
    expect(row.get_by_test_id("check-status")).to_have_text("Pendiente")
    expect(page.get_by_test_id("rules-table").locator("tr[data-rule='id_not_verified']")).to_contain_text("En espera")
    row.get_by_role("button", name="Marcar Verificación de identidad como válida").click()
    expect(row.get_by_test_id("check-status")).to_have_text("Válido")
    expect(page.get_by_test_id("outcome-banner")).to_contain_text("Pre-calificado por el asistente")

    # the candidate is told on their channel
    expect(candidate_page.locator(".bubble.agent").last).to_contain_text("cumples los requisitos", timeout=15_000)
    expect(candidate_page.locator(".outcome .tag")).to_have_text("Pre-calificado para entrevista", timeout=15_000)


def test_registry_failure_goes_to_review_and_can_be_resolved(page: Page, seed):
    s = seed(err=(construction_id_error, "Iker", "construction_laborer"))
    sid, _ = s["err"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=screening")
    expect(page.locator(".cand-actions").get_by_test_id("stage-pill")).to_contain_text("Por revisar")
    row = page.get_by_test_id("checks-table").locator("tr[data-check='id_check']")
    expect(row.get_by_test_id("check-status")).to_have_text("Error")
    expect(row).to_contain_text("Registro no disponible")
    expect(page.locator(".flag-list")).to_contain_text("Verificación fallida: Verificación de identidad")

    row.get_by_role("button", name="Reintentar").click()
    expect(page.locator(".toast").last).to_contain_text("Verificación reenviada")
    expect(row.get_by_test_id("check-status")).to_have_text("Error", timeout=15_000)  # same test number fails again

    row.get_by_role("button", name="Marcar Verificación de identidad como válida").click()
    expect(row.get_by_test_id("check-status")).to_have_text("Válido")
    expect(row).to_contain_text("por Ana Ruiz")
    expect(page.locator(".flag-list")).not_to_contain_text("Verificación fallida")
    expect(page.locator(".cand-actions").get_by_test_id("stage-pill")).to_contain_text("Pre-calificado")


def test_non_blocking_manual_licence_check(page: Page, seed):
    s = seed(ok=(courier_qualified, "Laura", None))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}?tab=screening")
    expect(page.get_by_test_id("outcome-banner")).to_contain_text("Pre-calificado")  # did not wait for it
    row = page.get_by_test_id("checks-table").locator("tr[data-check='license_check']")
    expect(row).to_contain_text("Manual (reclutador)")
    expect(row.get_by_test_id("check-status")).to_have_text("Pendiente")
    row.get_by_role("button", name="Marcar Comprobación del carnet como no válida").click()
    expect(row.get_by_test_id("check-status")).to_have_text("No válido")
    expect(page.locator(".flag-list")).to_contain_text("Carnet no verificado")
    expect(page.locator(".cand-actions").get_by_test_id("stage-pill")).to_contain_text("Por revisar")


def test_the_id_number_is_never_shown_in_clear(page: Page, seed):
    s = seed(ok=(construction_qualified, "Marta", "construction_laborer"))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    expect(page.get_by_test_id("value-id_document")).to_have_text("DNI •••••678Z")
    page.get_by_test_id("tab-conversation").click()
    expect(page.get_by_test_id("transcript")).to_contain_text("[id redacted]")
    expect(page.get_by_test_id("transcript")).not_to_contain_text(VALID_ID)
    page.get_by_test_id("tab-screening").click()
    expect(page.get_by_test_id("checks-table")).to_contain_text("Documento verificado en el registro")
    assert VALID_ID not in page.content()


def test_recruiter_correction_triggers_a_new_verification(page: Page, seed):
    s = seed(ok=(construction_qualified, "Sara", "construction_laborer"))
    sid, _ = s["ok"]
    page.goto(f"/backoffice#/candidates/{sid}")
    page.get_by_role("button", name="Editar Documento de identidad").click()
    dialog = page.get_by_role("dialog", name="Editar: Documento de identidad")
    dialog.get_by_label("Documento de identidad").fill("X1234567L")
    dialog.get_by_role("button", name="Guardar").click()
    expect(page.get_by_test_id("value-id_document")).to_have_text("NIE •••••567L")
    page.get_by_test_id("tab-screening").click()
    row = page.get_by_test_id("checks-table").locator("tr[data-check='id_check']")
    expect(row.get_by_test_id("check-status")).to_have_text("Válido", timeout=15_000)
    page.get_by_test_id("tab-activity").click()
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("Ana Ruiz corrigió datos")
    expect(timeline.locator(".ev").filter(has_text="Verificación solicitada: Verificación de identidad")).to_have_count(2)
    expect(timeline).to_contain_text("Resultado de Verificación de identidad: válido")
