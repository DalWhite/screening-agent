"""Analytics drill-down: every number opens the candidate list with the same
filters, and the list shows exactly that many candidates."""

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import construction_qualified, construction_underage, courier_no_license

pytestmark = pytest.mark.e2e


def open_analytics(page: Page, profile: str = "") -> None:
    page.goto("/backoffice#/analytics")
    page.get_by_label("Proceso de selección").select_option(profile)
    expect(page.get_by_test_id("stat-total")).to_be_visible()


def number_of(row) -> int:
    return int(row.locator(".num").inner_text())


def result_count(page: Page) -> int:
    text = page.get_by_test_id("result-count").inner_text()
    return int(re.match(r"(\d+)", text).group(1))


def test_chart_rows_open_the_matching_candidates(page: Page, seed):
    s = seed(ko=(construction_underage, "Lucía", "construction_laborer"))
    open_analytics(page, "construction_laborer")
    row = page.get_by_test_id("chart-Motivos de descarte").get_by_role("button", name=re.compile("^Menor de 18 años"))
    expected = number_of(row)
    row.click()
    expect(page).to_have_url(re.compile(r"#/candidates\?.*disqualify_reason=underage"))
    expect(page.get_by_test_id("active-filters")).to_contain_text("Motivo de descarte: Menor de 18 años")
    expect(page.get_by_test_id("active-filters")).to_contain_text("Resultado: No califica")
    expect(page.get_by_test_id("candidate-row").filter(has_text=s["ko"][1])).to_have_count(1)
    assert result_count(page) == expected


def test_kpis_and_funnel_drill_down(page: Page, seed):
    seed(ok=(construction_qualified, "Sara", "construction_laborer"))
    open_analytics(page, "construction_laborer")
    card = page.get_by_test_id("stat-qualified")
    expected = int(card.locator(".stat-num").inner_text())
    card.click()
    expect(page.get_by_test_id("active-filters")).to_contain_text("Resultado: Pre-calificado")
    expect(page.get_by_test_id("result-count")).to_contain_text(f"{expected} candidato")

    open_analytics(page, "construction_laborer")
    funnel = page.get_by_test_id("chart-Embudo por pregunta")
    step = funnel.get_by_role("button", name=re.compile("^Permiso de trabajo"))
    expected = number_of(step)
    step.click()
    expect(page.get_by_test_id("active-filters")).to_contain_text("Llegó a: Permiso de trabajo")
    expect(page.get_by_test_id("result-count")).to_contain_text(f"{expected} candidato")


def test_analytics_filters_travel_to_the_candidate_list(page: Page, seed):
    seed(ko=(courier_no_license, "Pedro", None))
    open_analytics(page)
    page.get_by_role("radio", name="7 días").click()
    page.get_by_label("Canal").select_option("web")
    expect(page).to_have_url(re.compile(r"period=7.*source=web|source=web.*period=7"))
    row = page.get_by_test_id("chart-Candidatos por proceso").get_by_role("button", name=re.compile("^Repartidor"))
    expected = number_of(row)
    row.click()
    active = page.get_by_test_id("active-filters")
    expect(active).to_contain_text("Canal: Chat web")
    expect(active).to_contain_text(re.compile(r"Desde \d{4}-\d{2}-\d{2}"))
    expect(page.get_by_label("Proceso de selección")).to_have_value("delivery_courier")
    expect(page.get_by_test_id("result-count")).to_contain_text(f"{expected} candidato")
