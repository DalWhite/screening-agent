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
from backend.screening.schema import Stage

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
    state = agent.new_session(language=spec["language"])
    agent.opening(state)
    used_rag = False
    for msg in spec["messages"]:
        _, decision = agent.process(state, msg, today=TODAY)
        if decision.rag_used:
            used_rag = True
    return state, used_rag


def main():
    results = []
    for name, spec in SCENARIOS.items():
        state, used_rag = run_scenario(spec)
        checks = {
            "stage": state.stage.value == spec["expect_stage"],
            "flags": all(flag in state.flags for flag in spec["expect_flags"]),
            "rag": used_rag == spec["expect_rag"],
            "no_empty_replies": all(
                m["content"].strip() for m in state.history if m["role"] == "assistant"
            ),
        }
        results.append({
            "scenario": name,
            "passed": all(checks.values()),
            "checks": checks,
            "stage": state.stage.value,
            "flags": state.flags,
            "turns": sum(1 for m in state.history if m["role"] == "user"),
            "qualified": bool(state.summary and state.summary["qualified"]),
        })

    passed = sum(1 for r in results if r["passed"])
    report = {
        "generated_for": "Grupo Sazon screening agent evals",
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
