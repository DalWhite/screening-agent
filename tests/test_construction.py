"""Low-qualification construction screening: the same engine, different business rules."""

from datetime import date

from backend.llm.client import MockLLM
from backend.pipeline import auto_stage
from backend.screening.agent import ScreeningAgent
from backend.screening.schema import ExtractionResult, Intent, Stage

TODAY = date(2026, 10, 11)
PROFILE = "construction_laborer"

VALID_ID = "12345678Z"    # checksum-valid DNI; the mock registry answers "valid"
UNKNOWN_ID = "99000001C"  # checksum-valid, but the mock registry answers "invalid" (test number 99…)

QUALIFIED = [
    "Hola, busco trabajo en obra",
    "Carlos Martín Ruiz",
    "sí, tengo 34 años",
    "sí, tengo NIE con permiso",
    f"mi DNI es {VALID_ID}",
    "Getafe",
    "sí, sin problema",
    "no, no lo tengo",
    "3 años de peón y algo de albañilería",
    "tengo coche",
    "el lunes",
]


def run(messages, language=None, llm=None, check_result="valid"):
    """Run a scripted conversation; external checks requested on the way are answered with
    ``check_result`` after the conversation (asynchronously, like the worker would)."""
    agent = ScreeningAgent(llm or MockLLM())
    state = agent.new_session(profile_id=PROFILE, language=language)
    opening = agent.opening(state)
    turns = [(None, opening, None)]
    requested = []
    for msg in messages:
        reply, decision = agent.process(state, msg, today=TODAY)
        requested.extend(decision.check_requests)
        turns.append((msg, reply, decision))
    if check_result:
        for req in requested:
            late, decision = agent.apply_check_result(state, req["check"], check_result, today=TODAY)
            if late:
                turns.append((None, late, decision))
    return agent, state, turns


def test_opening_uses_construction_persona():
    _, state, turns = run([])
    assert "Andrés" in turns[0][1] and "Constructora Horizonte" in turns[0][1]
    assert state.profile_id == PROFILE


def test_qualified_labourer_with_training_flag_and_score():
    _, state, _ = run(QUALIFIED)
    assert state.stage == Stage.QUALIFIED
    data = state.data
    assert data.city == "Madrid" and data.zone == "Getafe"
    assert data.is_adult is True and data.work_permit is True
    assert data.construction_experience_years == 3
    assert data.construction_trades == ["peón / ayudante", "albañilería"]
    assert data.transport == "own_vehicle"
    # missing PRL course is a flag for the recruiter, never a rejection
    assert "needs_prl_training" in state.flags
    assert any("PRL" in n for n in state.notes)
    assert state.summary["qualified"] is True
    assert state.summary["profile_id"] == PROFILE
    assert state.summary["score"] == 75.0
    assert auto_stage(state.status, state.flags, {"physical_review"}) == "qualified"


def test_underage_by_stated_age_is_knocked_out():
    _, state, turns = run(["hola", "Lucía Pérez", "tengo 17 años"])
    assert state.stage == Stage.DISQUALIFIED
    assert state.summary["disqualify_reason"] == "underage"
    assert "mayor de edad" in turns[-1][1]


def test_adult_by_stated_age():
    _, state, _ = run(["hola", "Lucía Pérez", "tengo 19"])
    assert state.data.is_adult is True and state.stage == "work_permit"


def test_no_work_permit_is_knocked_out():
    _, state, _ = run(["hola", "Ahmed Benali", "sí", "no tengo permiso todavía"])
    assert state.stage == Stage.DISQUALIFIED
    assert state.summary["disqualify_reason"] == "no_work_permit"


def test_out_of_area_gets_one_retry_then_waitlist():
    _, state, turns = run(["hola", "Iker Etxeberria", "sí", "sí", VALID_ID, "Bilbao"])
    assert state.stage == "location"
    assert "Bilbao" in turns[-1][1]  # the re-ask names the city the candidate typed
    _, state, _ = run(["hola", "Iker Etxeberria", "sí", "sí", VALID_ID, "Bilbao", "Vitoria"])
    assert state.stage == Stage.DISQUALIFIED
    assert state.summary["disqualify_reason"] == "out_of_area"
    assert "waitlist_out_of_area" in state.flags


def test_retry_into_covered_area_continues():
    _, state, _ = run(["hola", "Iker Etxeberria", "sí", "sí", VALID_ID, "Bilbao", "vale, Zaragoza"])
    assert state.stage == "physical" and state.data.city == "Zaragoza"


def test_physical_limitation_is_flagged_for_review_not_rejected():
    _, state, _ = run(["hola", "Rosa Gil", "sí", "sí", VALID_ID, "Sevilla, Dos Hermanas", "no puedo cargar tanto peso",
                       "sí", "0", "en autobús", "en 2 semanas"])
    assert state.stage == Stage.QUALIFIED
    assert "physical_review" in state.flags
    assert auto_stage(state.status, state.flags, {"physical_review"}) == "review"


def test_optional_questions_give_up_after_one_attempt():
    _, state, _ = run(["hola", "Rosa Gil", "sí", "sí", VALID_ID, "Toledo", "sí", "ni idea de qué es eso",
                       "mmm, quizás", "en autobús", "mañana"])
    assert state.stage == Stage.QUALIFIED
    assert {"prl", "experience"} <= set(state.given_up)
    assert "unresolved:prl" in state.flags


def test_english_conversation_stays_in_english():
    _, state, turns = run(["Hi, I want to apply", "John Smith", "yes", "yes", "X1234567L", "Malaga", "yes", "yes I have it",
                           "5 years as a bricklayer", "I drive my own car", "next week"], language="en")
    assert state.stage == Stage.QUALIFIED
    assert turns[2][1].startswith("Are you 18")  # a bare name did not flip the language
    assert state.data.city == "Málaga" and state.data.zone is None
    assert state.data.construction_trades == ["albañilería"]
    assert "site manager" in turns[-1][1]


def test_faq_answers_come_from_the_construction_knowledge_base():
    _, state, turns = run(["hola", "Carlos Martín", "¿Dais vosotros las botas y el casco?"])
    msg, reply, decision = turns[-1]
    assert decision.rag_used[0] == "ppe_es"
    assert "EPIs" in reply and "mayor de 18" in reply  # answer + pending question re-asked
    assert state.stage == "adult"


def test_reask_is_specific_to_the_question():
    _, state, turns = run(["hola", "Carlos Martín", "sí", "depende"])
    assert state.stage == "work_permit"
    assert "permiso de trabajo" in turns[-1][1]


class MultiFieldLLM(MockLLM):
    """A real LLM often extracts several answers from one message."""

    def extract(self, history, stage, today, profile=None):
        if stage == "name":
            return ExtractionResult(intent=Intent.ANSWER, fields={
                "full_name": "Marta Ruiz Soto", "is_adult": True, "work_permit": True,
                "city": "Valencia", "zone": "Paterna", "transport": "teleport",
            })
        return super().extract(history, stage, today, profile=profile)


def test_multiple_answers_in_one_message_skip_ahead():
    _, state, _ = run(["hola", "Soy Marta Ruiz Soto, 30 años, con papeles, vivo en Paterna"], llm=MultiFieldLLM())
    assert state.stage == "id_document"  # the ID is never taken from the model: it is asked explicitly
    assert state.data.city == "Valencia" and state.data.zone == "Paterna"
    # an invalid value for a later question is ignored, not applied
    assert state.data.transport is None


def test_courier_and_construction_sessions_coexist():
    agent = ScreeningAgent(MockLLM())
    courier = agent.new_session()
    labourer = agent.new_session(profile_id=PROFILE)
    assert "Marisol" in agent.opening(courier) and "Andrés" in agent.opening(labourer)
    for msg in ["hola", "Pedro Sánchez", "no tengo carnet"]:
        agent.process(courier, msg, today=TODAY)
    for msg in ["hola", "Pedro Sánchez", "sí", "sí", VALID_ID]:
        agent.process(labourer, msg, today=TODAY)
    assert courier.stage == Stage.DISQUALIFIED and courier.disqualify_reason == "no_license"
    assert labourer.stage == "location"  # no driving licence question for construction


def test_reopen_after_out_of_area_clears_the_knockout():
    agent, state, _ = run(["hola", "Iker Etxeberria", "sí", "sí", VALID_ID, "Bilbao", "Vitoria"], check_result=None)
    assert state.stage == Stage.DISQUALIFIED
    reply = agent.reopen(state)
    assert state.stage == "location" and state.status == "active"
    assert "waitlist_out_of_area" not in state.flags and "out_of_area" not in state.flags
    assert "ciudad" in reply
    agent.process(state, "Me mudo a Zaragoza", today=TODAY)
    assert state.stage == "physical"


def test_an_option_called_none_is_a_real_answer():
    # regression: "none" (no own transport) used to be discarded as a null value
    _, state, _ = run(["hola", "Ana Gil Mora", "sí", "sí", VALID_ID, "Toledo", "sí", "sí", "0", "voy andando"],
                      check_result=None)
    assert state.data.transport == "none" and "transport_review" in state.flags
