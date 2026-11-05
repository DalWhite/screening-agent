"""Objective 1 — one agent engine, several recruiting processes.

The candidate chat runs the low-qualification construction screening and the
original delivery-courier screening side by side, each with its own persona,
questions, knockout rules and FAQ.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import VALID_ID, say as answer, unique_surname

pytestmark = pytest.mark.e2e


def last_agent_message(page: Page):
    return page.locator(".bubble.agent:not(.typing)").last


def test_job_picker_offers_every_active_process(page: Page):
    page.goto("/")
    expect(page.get_by_role("heading", name="¿A qué oferta quieres aplicar?")).to_be_visible()
    courier = page.locator('.job[data-job="delivery_courier"]')
    construction = page.locator('.job[data-job="construction_laborer"]')
    expect(construction).to_contain_text("Peón / ayudante de obra")
    expect(construction).to_contain_text("Constructora Horizonte")
    expect(courier).to_contain_text("Repartidor/a (conductor)")
    expect(courier).to_contain_text("Grupo Sazón")

    construction.click()
    expect(page).to_have_url(re.compile(r"\?job=construction_laborer$"))
    expect(page.locator("#brand")).to_contain_text("Constructora Horizonte")
    expect(last_agent_message(page)).to_contain_text("Soy Andrés")
    expect(page.locator("#stage-chip")).to_have_text("Inicio")


def test_construction_candidate_is_prequalified(page: Page):
    name = f"Carlos {unique_surname()}"
    page.goto("/?job=construction_laborer")
    expect(last_agent_message(page)).to_contain_text("Constructora Horizonte")

    answer(page, "Hola, busco trabajo en obra")
    expect(last_agent_message(page)).to_contain_text("¿Cómo te llamas?")
    answer(page, name)
    expect(last_agent_message(page)).to_contain_text("mayor de 18")
    expect(page.locator("#stage-chip")).to_have_text("Mayor de edad · 2/10")
    answer(page, "sí, tengo 34 años")
    expect(last_agent_message(page)).to_contain_text("permiso para trabajar")
    answer(page, "sí, tengo NIE con permiso")
    expect(last_agent_message(page)).to_contain_text("DNI, NIE o pasaporte")
    answer(page, f"mi DNI es {VALID_ID}")
    answer(page, "Getafe")
    expect(last_agent_message(page)).to_contain_text("25 kg")
    answer(page, "sí, sin problema")
    expect(last_agent_message(page)).to_contain_text("PRL")
    answer(page, "no, no lo tengo")  # not a knockout: the company provides the course
    answer(page, "3 años de peón y algo de albañilería")
    expect(last_agent_message(page)).to_contain_text("vehículo propio o transporte público")
    answer(page, "tengo coche")
    answer(page, "el lunes")

    # the ID check answers asynchronously: the final decision arrives on its own
    expect(page.locator(".bubble.agent").filter(has_text="cumples los requisitos")).to_be_visible(timeout=15_000)
    expect(page.locator(".outcome .tag")).to_have_text("Pre-calificado para entrevista", timeout=15_000)
    expect(page.get_by_label("Tu respuesta")).to_be_disabled()


def test_construction_knockout_for_minors(page: Page):
    page.goto("/?job=construction_laborer")
    answer(page, "hola")
    answer(page, f"Lucía {unique_surname()}")
    answer(page, "tengo 17 años")
    expect(last_agent_message(page)).to_contain_text("hay que ser mayor de edad")
    expect(page.locator(".outcome .tag")).to_have_text("No califica para el puesto")
    expect(page.get_by_label("Tu respuesta")).to_be_disabled()


def test_construction_faq_is_answered_from_its_own_knowledge_base(page: Page):
    page.goto("/?job=construction_laborer")
    answer(page, "hola")
    answer(page, f"Ana {unique_surname()}")
    answer(page, "¿Dais vosotros las botas y el casco?")
    reply = last_agent_message(page)
    expect(reply).to_contain_text("EPIs sin coste")      # construction FAQ
    expect(reply).to_contain_text("mayor de 18")         # then re-asks the pending question


def test_out_of_area_gets_a_retry_then_the_waitlist(page: Page):
    page.goto("/?job=construction_laborer")
    for text in ["hola", f"Iker {unique_surname()}", "sí", "sí", VALID_ID]:
        answer(page, text)
    answer(page, "Bilbao")
    expect(last_agent_message(page)).to_contain_text("no tenemos obras en Bilbao")
    answer(page, "Vitoria")
    expect(last_agent_message(page)).to_contain_text("lista de espera")
    expect(page.locator(".outcome .tag")).to_have_text("No califica para el puesto")


def test_courier_process_keeps_its_own_rules(page: Page):
    """Regression: the original delivery screening still works unchanged."""
    page.goto("/?job=delivery_courier")
    expect(page.locator("#brand")).to_contain_text("Grupo Sazón")
    expect(last_agent_message(page)).to_contain_text("Soy Marisol")
    answer(page, "hola")
    answer(page, f"Laura {unique_surname()}")
    expect(last_agent_message(page)).to_contain_text("licencia de conducir")
    answer(page, "sí, tengo carnet")
    answer(page, "Madrid, Malasaña")
    answer(page, "jornada completa")
    answer(page, "flexible")
    answer(page, "2 años en Glovo")
    answer(page, "mañana")
    expect(page.locator(".outcome .tag")).to_have_text("Pre-calificado para entrevista")


def test_english_candidate_gets_the_english_screening(page: Page):
    page.goto("/?job=construction_laborer")
    answer(page, "Hi, I want to apply for the labourer job")
    answer(page, f"John {unique_surname()}")
    expect(last_agent_message(page)).to_have_text("Are you 18 or older?")


def test_session_survives_a_reload(page: Page):
    page.goto("/?job=construction_laborer")
    answer(page, "hola")
    name = f"Rosa {unique_surname()}"
    answer(page, name)
    page.reload()
    expect(page.locator(".bubble.user").filter(has_text=name)).to_be_visible()
    expect(page.locator("#stage-chip")).to_have_text("Mayor de edad · 2/10")
    answer(page, "sí")
    expect(last_agent_message(page)).to_contain_text("permiso para trabajar")
