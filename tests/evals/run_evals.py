# -*- coding: utf-8 -*-
"""Scripted conversation evals: run fixed candidate scripts through the
agent (MockLLM) and score outcomes. Writes tests/evals/report.json.

Run:  python tests/evals/run_evals.py
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.llm.client import MockLLM
from backend.screening.agent import ScreeningAgent

TODAY = date(2026, 10, 11)

SCENARIOS = {
    "qualified_es_faq": {
        "language": None,
        "messages": [
            "Hola, quiero aplicar para repartidor",
            "Laura Jiménez Ortiz",
            "¿Cuánto pagan por hora?",
            "sí, tengo carnet",
            "Madrid, Malasaña",
            "jornada completa",
            "mañana",
            "2 años en Glovo",
            "Puedo empezar el 2026-11-02",
        ],
        "expect_stage": "qualified",
        "expect_flags": [],
        "expect_rag": True,
    },
    "qualified_en_codeswitch": {
        "language": "en",
        "messages": [
            "Hi, I would like to apply",
            "Mark Lee",
            "yes",
            "Barcelona, Spain",
            "part time",
            "por la mañana, morning",
            "1 year in Uber Eats",
            "in 2 weeks",
        ],
        "expect_stage": "qualified",
        "expect_flags": [],
        "expect_rag": False,
    },
    "disqualified_no_license": {
        "language": None,
        "messages": ["hola", "Pedro Sánchez", "no tengo carnet"],
        "expect_stage": "disqualified",
        "expect_flags": [],
        "expect_rag": False,
    },
    "disqualified_out_of_area": {
        "language": None,
        "messages": ["hola", "Ana Ruiz", "sí", "Ourense", "Vigo"],
        "expect_stage": "disqualified",
        "expect_flags": ["waitlist_out_of_area"],
        "expect_rag": False,
    },
    "giveup_needs_review": {
        "language": None,
        "messages": [
            "hola", "Jonas Vidal", "sí", "Valencia, Ruzafa",
            "depende", "quizá", "no sé",
            "flexible", "no tengo experiencia", "la próxima semana",
        ],
        "expect_stage": "abandoned",
        "expect_flags": ["needs_review", "unresolved:availability"],
        "expect_rag": False,
    },
    "construction_qualified_prl_flag": {
        "profile": "construction_laborer",
        "language": None,
        "messages": [
            "Hola, busco trabajo en obra", "Carlos Martín Ruiz", "sí, tengo 34 años", "sí, tengo NIE con permiso",
            "mi DNI es 12345678Z", "Getafe", "sí, sin problema", "no, no lo tengo", "3 años de peón y algo de albañilería",
            "tengo coche", "el lunes",
        ],
        "expect_stage": "qualified",
        "expect_flags": ["needs_prl_training"],
        "expect_rag": False,
    },
    "construction_ppe_question": {
        "profile": "construction_laborer",
        "language": None,
        "messages": ["hola", "Ana Torres Gil", "¿Dais vosotros las botas y el casco?", "sí", "sí", "X1234567L", "Zaragoza",
                     "sí", "sí", "0", "en autobús", "en 2 semanas"],
        "expect_stage": "qualified",
        "expect_flags": [],
        "expect_rag": True,
    },
    "construction_underage": {
        "profile": "construction_laborer",
        "language": None,
        "messages": ["hola", "Lucía Pérez", "tengo 17 años"],
        "expect_stage": "disqualified",
        "expect_flags": ["underage"],
        "expect_rag": False,
    },
    "construction_no_work_permit": {
        "profile": "construction_laborer",
        "language": None,
        "messages": ["hola", "Ahmed Benali", "sí", "no tengo permiso todavía"],
        "expect_stage": "disqualified",
        "expect_flags": ["no_work_permit"],
        "expect_rag": False,
    },
    "construction_physical_review_en": {
        "profile": "construction_laborer",
        "language": "en",
        "messages": ["Hi, I want to apply", "John Smith", "yes", "yes", "Y1234567X", "Malaga", "no, I can't lift heavy loads",
                     "yes I have it", "5 years as a bricklayer", "I drive my own car", "next week"],
        "expect_stage": "qualified",
        "expect_flags": ["physical_review"],
        "expect_rag": False,
    },
    "construction_id_not_in_registry": {
        "profile": "construction_laborer",
        "language": None,
        "messages": ["hola", "Iván Soto Gil", "sí", "sí", "99000001C", "Madrid"],
        "check_results": "registry",
        "expect_stage": "disqualified",
        "expect_flags": ["id_not_verified"],
        "expect_rag": False,
    },
    "abuse_abandoned": {
        "language": None,
        "messages": ["hola", "mierda", "mierda otra vez", "esto es una mierda"],
        "expect_stage": "abandoned",
        "expect_flags": ["abusive_language"],
        "expect_rag": False,
    },
}


def run_scenario(spec):
    agent = ScreeningAgent(MockLLM())
    state = agent.new_session(language=spec["language"], profile_id=spec.get("profile"))
    agent.opening(state)
    used_rag = False
    requests = []
    for msg in spec["messages"]:
        _, decision = agent.process(state, msg, today=TODAY)
        requests.extend(decision.check_requests)
        if decision.rag_used:
            used_rag = True
    # external checks answer asynchronously: the mock registry's test numbers decide the result
    from backend.verifications.providers import MockIdRegistry

    for req in requests:
        status, detail = MockIdRegistry(delay_seconds=0).poll({"due_at": None}, req["payload"], None)
        agent.apply_check_result(state, req["check"], status, detail, today=TODAY)
    return state, used_rag


def main():
    results = []
    for name, spec in SCENARIOS.items():
        state, used_rag = run_scenario(spec)
        checks = {
            "stage": state.stage == spec["expect_stage"],
            "flags": all(flag in state.flags for flag in spec["expect_flags"]),
            "rag": used_rag == spec["expect_rag"],
            "no_empty_replies": all(
                m["content"].strip() for m in state.history if m["role"] == "assistant"
            ),
        }
        results.append({
            "scenario": name,
            "profile": state.profile_id,
            "passed": all(checks.values()),
            "checks": checks,
            "stage": state.stage,
            "flags": state.flags,
            "turns": sum(1 for m in state.history if m["role"] == "user"),
            "qualified": bool(state.summary and state.summary["qualified"]),
            "score": state.summary and state.summary.get("score"),
        })

    passed = sum(1 for r in results if r["passed"])
    report = {
        "generated_for": "Screening agent evals (all job profiles)",
        "llm": "MockLLM (deterministic; run against live model for LLM evals)",
        "total": len(results),
        "passed": passed,
        "results": results,
    }
    out = Path(__file__).resolve().parent / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['scenario']} stage={r['stage']} flags={r['flags']}")
    print(f"TOTAL: {passed}/{len(results)} — report written to {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
