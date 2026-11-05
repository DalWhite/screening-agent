"""Objective 2.4 — advanced search and filtering.

Free-text search (accent-insensitive, also over the conversation and recruiter
notes), faceted filters, score/date ranges, quick views, sorting, shareable
URLs, bulk actions and CSV export.
"""

import csv
import io
import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import (
    construction_physical, construction_qualified, construction_underage, courier_qualified, unique_surname,
)

pytestmark = pytest.mark.e2e


def rows(page: Page):
    return page.get_by_test_id("candidate-row")


def search(page: Page, text: str) -> None:
    page.get_by_test_id("search").fill(text)


def pick(page: Page, filter_key: str, option: str, check: bool = True) -> None:
    """Open a filter chip's popover and (un)tick one option."""
    page.get_by_test_id(f"filter-{filter_key}").click()
    box = page.locator(".popover").get_by_role("checkbox", name=option, exact=True)
    box.check() if check else box.uncheck()
    page.keyboard.press("Escape")


def more_filters(page: Page) -> None:
    if page.get_by_test_id("more-filters").is_hidden():
        page.get_by_test_id("more-filters-toggle").click()


@pytest.fixture
def cohort(seed):
    """Four candidates across both processes sharing a unique surname-tag."""
    tag = unique_surname()
    people = seed(
        carlos=(construction_qualified, f"Carlos {tag}", "construction_laborer"),
        rosa=(construction_physical, f"Rosa {tag}", "construction_laborer"),
        lucia=(construction_underage, f"Lucía {tag}", "construction_laborer"),
        laura=(courier_qualified, f"Laura {tag}", None),
    )
    return tag, people


@pytest.fixture
def candidates_page(page: Page) -> Page:
    page.goto("/backoffice#/candidates")
    page.get_by_label("Proceso de selección").select_option("")
    return page


def test_free_text_search_is_accent_insensitive(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    search(page, f"lucia {tag.lower()}")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["lucia"][1])
    expect(page.get_by_test_id("result-count")).to_have_text("1 candidato")


def test_search_inside_conversations(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    search(page, f"{tag} albañilería")  # only Carlos said it in the chat
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["carlos"][1])


def test_filters_combine_and_live_in_the_url(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    search(page, tag)
    expect(rows(page)).to_have_count(4)

    page.get_by_label("Proceso de selección").select_option("construction_laborer")
    expect(rows(page)).to_have_count(3)
    pick(page, "pipeline_stage", "Pre-calificado")            # stage filter
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["carlos"][1])
    expect(page.get_by_test_id("active-filters")).to_contain_text("Etapa: Pre-calificado")

    page.get_by_role("button", name="Quitar filtro Etapa: Pre-calificado").click()
    pick(page, "flag", "Revisar capacidad física")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["rosa"][1])
    expect(page).to_have_url(re.compile(r"flag=physical_review"))

    # a shared link restores the same filtered view
    url = page.url
    page.goto("about:blank")
    page.goto(url)
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["rosa"][1])
    expect(page.get_by_label("Proceso de selección")).to_have_value("construction_laborer")

    page.get_by_test_id("clear-filters").click()
    search(page, tag)
    expect(rows(page)).to_have_count(3)


def test_score_range_and_sorting(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    search(page, tag)
    page.get_by_label("Ordenar por").select_option("score:desc")
    expect(rows(page).first).to_contain_text(people["laura"][1])    # 90
    expect(rows(page).nth(1)).to_contain_text(people["carlos"][1])  # 75
    expect(page.get_by_test_id("more-filters")).to_be_hidden()      # rarely used filters stay folded
    more_filters(page)
    page.get_by_label("Puntuación mínima").fill("50")
    expect(rows(page)).to_have_count(2)
    page.get_by_label("Puntuación máxima").fill("80")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["carlos"][1])


def test_quick_views(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    page.get_by_role("tab", name="Por revisar").click()
    search(page, tag)
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(people["rosa"][1])
    page.get_by_role("tab", name="Pre-calificados").click()
    expect(page.get_by_role("tab", name="Pre-calificados")).to_have_attribute("aria-selected", "true")
    search(page, tag)
    expect(rows(page)).to_have_count(2)


def test_change_stage_inline_from_the_list(candidates_page: Page, cohort, api):
    page = candidates_page
    tag, people = cohort
    search(page, f"laura {tag.lower()}")
    expect(rows(page)).to_have_count(1)
    rows(page).first.get_by_test_id("stage-pill").click()
    page.get_by_test_id("move-offer").click()
    expect(rows(page).first.get_by_test_id("stage-pill")).to_contain_text("Oferta")
    assert api.get(f"/api/bo/candidates/{people['laura'][0]}").json()["pipeline_stage"] == "offer"


def test_global_search_opens_the_quick_view(page: Page, cohort):
    tag, people = cohort
    page.goto("/backoffice#/pipeline")
    page.keyboard.press("/")
    page.get_by_test_id("global-search").fill(f"rosa {tag.lower()}")
    page.locator(".search-results a").first.click()
    expect(page.get_by_test_id("quick-view").get_by_role("heading", name=people["rosa"][1])).to_be_visible()


def test_bulk_move_and_tag(candidates_page: Page, cohort, api):
    page = candidates_page
    tag, people = cohort
    search(page, tag)
    expect(rows(page)).to_have_count(4)
    for key in ("carlos", "laura"):
        page.get_by_label(f"Seleccionar {people[key][1]}").check()
    bar = page.get_by_test_id("bulk-bar")
    expect(bar).to_contain_text("2 seleccionado(s)")
    bar.get_by_label("Etiquetar seleccionados").fill(f"campaña-{tag}")
    bar.get_by_role("button", name="Etiquetar").click()
    expect(rows(page).filter(has_text=f"#campaña-{tag}")).to_have_count(2)

    for key in ("carlos", "laura"):
        page.get_by_label(f"Seleccionar {people[key][1]}").check()
    bar.get_by_label("Mover seleccionados a").select_option("shortlisted")
    expect(page.locator(".toast").last).to_contain_text("2 candidato(s) movidos a «Preseleccionado»")
    expect(rows(page).filter(has_text="Preseleccionado")).to_have_count(2)
    assert api.get(f"/api/bo/candidates/{people['carlos'][0]}").json()["pipeline_stage"] == "shortlisted"


def test_export_filtered_candidates_to_csv(candidates_page: Page, cohort):
    page = candidates_page
    tag, people = cohort
    page.get_by_label("Proceso de selección").select_option("construction_laborer")
    search(page, tag)
    expect(rows(page)).to_have_count(3)
    with page.expect_download() as download_info:
        page.get_by_test_id("export-csv").click()
    content = open(download_info.value.path(), encoding="utf-8").read()
    exported = {r["name"]: r for r in csv.DictReader(io.StringIO(content))}
    assert set(exported) == {people[k][1] for k in ("carlos", "rosa", "lucia")}
    assert exported[people["carlos"][1]]["transport"] == "own_vehicle"
    assert exported[people["lucia"][1]]["disqualify_reason"] == "underage"


def test_pipeline_board_search_and_process_filter(page: Page, cohort):
    tag, people = cohort
    page.goto("/backoffice#/pipeline")
    page.get_by_label("Proceso de selección").select_option("delivery_courier")
    page.get_by_label("Buscar en el pipeline").fill(tag)
    expect(page.get_by_test_id("candidate-card")).to_have_count(1)
    expect(page.get_by_test_id("candidate-card")).to_contain_text(people["laura"][1])
