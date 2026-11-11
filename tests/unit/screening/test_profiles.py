"""Job profiles: loading, validation of business rules, rule operators and scoring."""

import copy
import json
from datetime import date

import pytest

from pathlib import Path

from backend.config import settings
from backend.screening.profile import (
    JobProfile, ProfileRegistry, evaluate, get_profile, load_profile, load_seed, registry, validate_profile_data,
)

TODAY = date(2026, 10, 11)


def _raw(profile_id: str = "construction_laborer") -> dict:
    return load_seed(Path(settings.profiles_dir) / profile_id / "profile.json")


def test_registry_loads_every_profile():
    ids = {p.id for p in registry().all()}
    assert {"delivery_courier", "construction_laborer"} <= ids
    assert registry().default().id == "delivery_courier"


def test_profiles_have_independent_flows_and_knowledge():
    courier = get_profile("delivery_courier")
    construction = get_profile("construction_laborer")
    assert courier.flow == ["intro", "name", "license", "location", "availability", "schedule",
                            "experience", "start_date", "review"]
    assert construction.flow[1:4] == ["name", "adult", "work_permit"]
    assert "Getafe" not in courier.areas.cities() and "Toledo" in construction.areas.cities()
    assert construction.faq_index.search("¿Dais las botas y el casco?", lang="es")[0].id == "ppe_es"
    assert courier.faq_index.search("¿Cuánto pagan por hora?", lang="es")[0].id == "pay_es"


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        get_profile("astronaut")


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["rules"].append({"id": "xx_rule", "label": {"es": "x"}, "field": "nope", "op": "eq", "value": 1,
                                  "action": "flag"}), "dato desconocido «nope»"),
    (lambda d: d["rules"][0].pop("message"), "necesita un mensaje"),
    (lambda d: d["questions"].append(copy.deepcopy(d["questions"][0])), "id de pregunta repetido"),
    (lambda d: d["questions"][-2].update(options=[]), "no tiene ninguna"),
    (lambda d: d["rules"][0].update(op="approximately"), "operador desconocido"),
    (lambda d: d["rules"][0].update(on_error="out_of_area"), "solo uno"),
    (lambda d: d["scoring"].append({"field": "salary", "label": {"es": "x"}, "points": 1, "op": "eq", "value": 1}),
     "dato desconocido «salary»"),
    (lambda d: d["questions"][0].update(id="review"), "reservado"),
    (lambda d: d["questions"][0].update(requierd=True), "Extra inputs"),
    (lambda d: d["checks"].append({"id": "bad", "label": {"es": "x"}, "trigger": "nowhere"}), "no existe"),
])
def test_broken_profiles_fail_fast(mutate, message):
    raw = _raw()
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        JobProfile.model_validate(raw)


def test_editor_validation_reports_every_problem_with_its_cell():
    raw = _raw()
    raw["rules"][0]["field"] = "nope"
    raw["scoring"][0]["field"] = "salary"
    raw["questions"][1]["type"] = "teleport"
    profile, problems = validate_profile_data(raw)
    assert profile is None
    assert {"path": ["questions", 1, "type"], "message": "valor no permitido"} in problems
    raw["questions"][1]["type"] = "yes_no"  # structurally valid again: cross-section problems are all listed
    profile, problems = validate_profile_data(raw)
    paths = [p["path"] for p in problems]
    assert ["rules", 0, "field"] in paths and ["scoring", 0, "field"] in paths
    assert profile is None
    ok, none = validate_profile_data(_raw())
    assert ok is not None and none == []


def test_sensitive_inputs_only_from_their_own_question():
    raw = _raw()
    raw["checks"].append({"id": "late_check", "label": {"es": "x"}, "trigger": "@complete", "inputs": ["id_document"]})
    _, problems = validate_profile_data(raw)
    assert any("dato sensible" in p["message"] for p in problems)


def test_registry_rejects_missing_knowledge_files(tmp_path):
    raw = json.loads((Path(settings.profiles_dir) / "construction_laborer" / "profile.json").read_text(encoding="utf-8"))
    raw["faq_file"] = "missing.json"
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "profile.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="missing file"):
        load_profile(folder / "profile.json")


def test_new_profile_is_just_a_folder(tmp_path):
    """A new recruiting process needs no code: copy a folder, change the JSON."""
    raw = _raw()
    raw["id"] = "warehouse_picker"
    raw["company"] = "Almacenes Norte"
    raw["faq"] = []
    raw["service_areas"] = []
    folder = tmp_path / "warehouse_picker"
    folder.mkdir()
    (folder / "profile.json").write_text(json.dumps(raw), encoding="utf-8")
    reg = ProfileRegistry(tmp_path)
    profile = reg.get("warehouse_picker")
    assert profile.company == "Almacenes Norte" and profile.faq_index is None and profile.areas is None


@pytest.mark.parametrize("op, actual, expected, result", [
    ("eq", False, False, True),
    ("eq", None, False, False),          # unanswered never triggers a rule
    ("ne", "a", "b", True),
    ("lt", 17, 18, True),
    ("gte", 18, 18, True),
    ("in", "morning", ["morning", "evening"], True),
    ("not_in", "night", ["morning"], True),
    ("contains", ["full_time", "weekends"], "full_time", True),
    ("not_contains", ["weekends"], "full_time", True),
    ("missing", None, None, True),
    ("present", "x", None, True),
    ("within_days", "2026-10-15", 7, True),
    ("within_days", "2026-11-15", 7, False),
])
def test_rule_operators(op, actual, expected, result):
    assert evaluate(op, actual, expected, TODAY) is result


def test_knockout_and_qualification():
    p = get_profile("construction_laborer")
    assert p.knockout({"is_adult": False}).id == "underage"
    assert p.knockout({"is_adult": True, "work_permit": False}).id == "no_work_permit"
    assert p.knockout({"is_adult": True, "work_permit": True}) is None
    complete = {"full_name": "Ana Gil", "is_adult": True, "work_permit": True, "id_document": "•••••678Z", "city": "Madrid",
                "physical_ok": False, "transport": "none", "start_date": "2026-10-20"}
    # optional questions (PRL, experience) are not needed; flag rules never disqualify
    assert p.is_qualified(complete, TODAY)
    assert p.missing_required({"full_name": "Ana Gil"}) == ["is_adult", "work_permit", "id_document", "city",
                                                            "physical_ok", "transport", "start_date"]
    # an external check result is just data for the rules
    assert p.knockout({**complete, "id_check": "invalid"}).id == "id_not_verified"
    assert p.knockout({**complete, "id_check": "pending"}) is None


def test_scoring_is_normalised_and_explained():
    p = get_profile("construction_laborer")
    best = {"has_prl_course": True, "construction_experience_years": 10, "construction_trades": ["a", "b", "c"],
            "transport": "own_vehicle", "physical_ok": True, "start_date": TODAY.isoformat()}
    score, breakdown = p.score(best, TODAY)
    assert score == 100.0
    worst, _ = p.score({"transport": "none", "physical_ok": False}, TODAY)
    assert worst == 0.0
    mid, breakdown = p.score({"transport": "public_transport", "construction_experience_years": 2}, TODAY)
    assert mid == 20.0
    assert {"field": "transport", "points": 10.0} .items() <= next(
        b for b in breakdown if b["field"] == "transport" and b["points"]).items()


def test_review_flags_come_from_profile():
    assert get_profile("construction_laborer").review_flags() == {"physical_review"}
    assert get_profile("delivery_courier").review_flags() == {"license_not_verified"}


def test_public_view_exposes_rules_but_not_files():
    view = get_profile("construction_laborer").public()
    assert view["agent_name"] == "Andrés"
    assert [q["id"] for q in view["questions"]][:3] == ["name", "adult", "work_permit"]
    assert {r["id"] for r in view["rules"]} >= {"underage", "no_work_permit", "out_of_area", "physical_review"}
    assert "Toledo" in view["service_areas"] and view["revision"] == 1
