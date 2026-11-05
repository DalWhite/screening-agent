"""External (asynchronous) checks at the engine level: ID documents, blocking vs
non-blocking checks, the verifying outcome and rules reacting to late results."""

import json
from datetime import date

import pytest

from backend.llm.client import MockLLM
from backend.pipeline import auto_stage
from backend.screening.agent import ScreeningAgent
from backend.screening.fields import classify_document, document_checksum_ok, find_document, mask_document
from backend.screening.profile import JobProfile, get_profile
from backend.screening.schema import Stage

TODAY = date(2026, 10, 11)
BEFORE_ID = ["hola", "Carlos Martín Ruiz", "sí", "sí"]
AFTER_ID = ["Getafe", "sí", "no", "3 años de albañil", "tengo coche", "el lunes"]


class _OneProfile:
    def __init__(self, profile):
        self.profile = profile

    def get(self, profile_id=None, revision=None):
        return self.profile

    def default(self):
        return self.profile

    def all(self, include_inactive=True):
        return [self.profile]


def start(profile="construction_laborer", agent=None):
    agent = agent or ScreeningAgent(MockLLM())
    state = agent.new_session(profile_id=profile)
    agent.opening(state)
    return agent, state


def say(agent, state, messages):
    decisions = []
    for text in messages:
        decisions.append(agent.process(state, text, today=TODAY))
    return decisions


# ---------- identity documents ----------

@pytest.mark.parametrize("text, expected, kind", [
    ("mi dni es 12345678z", "12345678Z", "dni"),
    ("12.345.678-Z", "12345678Z", "dni"),
    ("NIE: Y-1234567-X", "Y1234567X", "nie"),
    ("pasaporte AAA123456", "AAA123456", "passport"),
])
def test_documents_are_found_and_classified(text, expected, kind):
    assert find_document(text) == expected and classify_document(expected) == kind
    assert document_checksum_ok(expected, kind)


def test_control_letter_and_mask():
    assert not document_checksum_ok("12345678A", "dni")
    assert not document_checksum_ok("X1234567A", "nie")
    assert mask_document("12345678Z") == "•••••678Z"


def test_id_question_reasks_with_specific_errors():
    agent, state = start()
    say(agent, state, BEFORE_ID)
    assert state.stage == "id_document"
    reply, decision = say(agent, state, ["12345678A"])[0]
    assert decision.validation_error == "invalid_id_checksum" and "letra no coincide" in reply
    reply, decision = say(agent, state, ["no sé, 1234"])[0]
    assert decision.validation_error == "invalid_id"
    assert state.stage == "id_document"


def test_valid_id_is_masked_and_queued_for_verification_never_stored_in_clear():
    agent, state = start()
    say(agent, state, BEFORE_ID)
    reply, decision = say(agent, state, ["claro, es el 12345678Z"])[0]
    assert state.stage == "location"
    assert state.data.id_document == "•••••678Z" and state.data.id_document_type == "dni"
    assert state.data.id_check == "pending"
    # the full number only travels in the (never persisted) check request
    assert decision.check_requests == [{"check": "id_check",
                                        "payload": {"id_document": "12345678Z", "full_name": "Carlos Martín Ruiz"}}]
    assert "12345678Z" not in json.dumps(state.history) + json.dumps(decision.events) + json.dumps(dict(state.data))
    assert "12345678Z" not in decision.model_dump_json()
    assert any(e["type"] == "check_requested" for e in decision.events)


def test_blocking_check_holds_the_decision_until_it_answers():
    agent, state = start()
    say(agent, state, BEFORE_ID + ["12345678Z"] + AFTER_ID)
    assert state.stage == Stage.VERIFYING and state.status == "verifying"
    assert "verificando" in state.history[-1]["content"]
    assert state.summary["outcome"] == "verifying" and state.summary["qualified"] is False
    assert auto_stage(state.status, state.flags) == "screening"
    # the candidate writes while waiting: polite holding answer, nothing changes
    reply, _ = agent.process(state, "¿ya está?", today=TODAY)
    assert "verificando" in reply and state.stage == Stage.VERIFYING

    reply, decision = agent.apply_check_result(state, "id_check", "valid", "ok", today=TODAY)
    assert state.stage == Stage.QUALIFIED and "cumples los requisitos" in reply
    assert state.summary["qualified"] is True and state.data.id_check == "valid"
    assert any(e["type"] == "check_result" for e in decision.events)


def test_invalid_document_knocks_out_even_mid_conversation():
    agent, state = start()
    say(agent, state, BEFORE_ID + ["99000001C", "Getafe"])
    assert state.stage == "physical"
    reply, _ = agent.apply_check_result(state, "id_check", "invalid", "no consta", today=TODAY)
    assert state.stage == Stage.DISQUALIFIED and state.disqualify_reason == "id_not_verified"
    assert "No hemos podido verificar tu documento" in reply
    # a later answer never re-qualifies a knocked-out candidate
    assert agent.apply_check_result(state, "id_check", "valid", today=TODAY)[0] is None
    assert state.stage == Stage.DISQUALIFIED


def test_provider_error_qualifies_but_routes_to_human_review():
    agent, state = start()
    say(agent, state, BEFORE_ID + ["98000001Z"] + AFTER_ID)
    reply, _ = agent.apply_check_result(state, "id_check", "error", "timeout", today=TODAY)
    assert state.stage == Stage.QUALIFIED
    assert "check_error:id_check" in state.flags
    assert auto_stage(state.status, state.flags) == "review"
    # a recruiter verifies the document by hand: the alert is resolved
    agent.apply_check_result(state, "id_check", "valid", today=TODAY)
    assert "check_error:id_check" not in state.flags and auto_stage(state.status, state.flags) == "qualified"


def test_reopening_an_id_knockout_asks_for_the_document_again():
    agent, state = start()
    say(agent, state, BEFORE_ID + ["99000001C"])
    agent.apply_check_result(state, "id_check", "invalid", today=TODAY)
    reply = agent.reopen(state)
    assert state.stage == "id_document" and "DNI" in reply
    assert state.data.id_check is None and state.data.id_document is None
    _, decision = agent.process(state, "perdona, es X1234567L", today=TODAY)
    assert state.data.id_check == "pending" and decision.check_requests[0]["payload"]["id_document"] == "X1234567L"


def test_non_blocking_manual_check_with_condition():
    agent, state = start("delivery_courier")
    decisions = say(agent, state, ["hola", "Laura Jiménez Ortiz", "sí, tengo carnet"])
    assert decisions[-1][1].check_requests[0]["check"] == "license_check"
    assert state.data.license_check == "pending"
    say(agent, state, ["Madrid", "jornada completa", "flexible", "2 años", "mañana"])
    assert state.stage == Stage.QUALIFIED  # non-blocking: the decision does not wait
    agent.apply_check_result(state, "license_check", "invalid", today=TODAY)
    assert state.stage == Stage.QUALIFIED and "license_not_verified" in state.flags
    assert auto_stage(state.status, state.flags, {"license_not_verified"}) == "review"


def test_condition_false_means_no_check():
    agent, state = start("delivery_courier")
    decisions = say(agent, state, ["hola", "Pedro Sánchez", "no tengo carnet"])
    assert decisions[-1][1].check_requests == [] and state.data.license_check is None


def test_on_complete_checks_fire_once_all_questions_are_answered():
    raw = get_profile("delivery_courier").to_data()
    raw["checks"].append({"id": "background_check", "label": {"es": "Antecedentes"}, "provider": "webhook",
                          "trigger": "@complete", "inputs": ["full_name", "city"], "blocking": True})
    raw["rules"].append({"id": "bad_background", "label": {"es": "x"}, "field": "background_check", "op": "eq",
                         "value": "invalid", "message": {"es": "Lo sentimos."}})
    profile = JobProfile.model_validate(raw)
    agent = ScreeningAgent(MockLLM(), _OneProfile(profile))
    state = agent.new_session(profile_id=profile.id)
    agent.opening(state)
    decisions = say(agent, state, ["hola", "Laura Jiménez Ortiz", "sí", "Madrid", "jornada completa", "flexible"])
    assert [r["check"] for d in decisions for r in d[1].check_requests] == ["license_check"]
    reply, decision = agent.process(state, "0", today=TODAY)  # experience
    reply, decision = agent.process(state, "mañana", today=TODAY)
    assert decision.check_requests == [{"check": "background_check",
                                        "payload": {"full_name": "Laura Jiménez Ortiz", "city": "Madrid"}}]
    assert state.stage == Stage.VERIFYING
    reply, _ = agent.apply_check_result(state, "background_check", "invalid", today=TODAY)
    assert state.stage == Stage.DISQUALIFIED and reply == "Lo sentimos."


def test_the_model_can_never_set_ids_or_check_results():
    class Liar(MockLLM):
        def extract(self, history, stage, today, profile=None):
            result = super().extract(history, stage, today, profile=profile)
            return result

    from backend.llm.client import _to_extraction

    profile = get_profile("construction_laborer")
    ex = _to_extraction({"intent": "answer", "language": "es", "sentiment": "neutral",
                         "id_document": "12345678Z", "id_check": "valid", "work_permit": True}, profile)
    assert ex.fields == {"work_permit": True}
    from backend.screening.prompts import extraction_tool, system_prompt

    props = extraction_tool(profile)["function"]["parameters"]["properties"]
    assert "id_document" not in props and "id_check" not in props
    assert "identity document number when the plan explicitly tells you" in system_prompt(profile)
    assert "never ask for documents, ID numbers" in system_prompt(get_profile("delivery_courier"))
