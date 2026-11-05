import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm.client import MockLLM
from backend.screening.agent import ScreeningAgent

TODAY = date(2026, 10, 11)

# (file, title, messages[, profile_id])
SCENARIOS = [
    (
        "qualified_spanish_faq.md",
        "Qualified candidate (ES) — asks a pay question answered from the FAQ",
        ["Hola, quiero aplicar para repartidor",
         "Laura Jiménez Ortiz",
         "¿Cuánto pagan por hora?",
         "sí, tengo carnet",
         "Madrid, Malasaña",
         "jornada completa",
         "mañana",
         "2 años en Glovo",
         "Puedo empezar el 2026-11-02. Mi teléfono es 600112233"],
    ),
    (
        "qualified_english_code_switch.md",
        "Qualified candidate (EN→ES code-switch mid-conversation)",
        ["Hi, I'd like to apply as a courier",
         "Daniel O'Brien",
         "yes, I have a valid driving license",
         "Valencia, Ruzafa",
         "tengo disponibilidad completa",
         "flexible, me da igual",
         "1 year with Uber Eats",
         "I can start on 15 November 2026"],
    ),
    (
        "disqualified_no_license.md",
        "Disqualified — no driver's license (soft landing with bike-courier waitlist)",
        ["hola", "Pedro Sánchez", "no tengo carnet"],
    ),
    (
        "disqualified_out_of_area.md",
        "Disqualified — city outside service areas after one retry (waitlist)",
        ["hola", "Ana Ruiz", "sí", "Ourense", "Vigo"],
    ),
    (
        "abandoned_needs_review.md",
        "Needs review — candidate gives up on a field after 3 re-asks",
        ["hola", "Marc Puig", "sí", "Barcelona, Gràcia",
         "no sé", "depende", "creo que a veces"],
    ),
    (
        "construction_qualified_prl_flag.md",
        "Construction labourer — qualified, no PRL course (flag for the recruiter, not a rejection)",
        ["Hola, busco trabajo en obra", "Carlos Martín Ruiz", "sí, tengo 34 años", "sí, tengo NIE con permiso",
         "mi DNI es 12345678Z", "Getafe", "¿Dais vosotros las botas y el casco?", "sí, sin problema", "no, no lo tengo",
         "3 años de peón y algo de albañilería", "tengo coche", "el lunes"],
        "construction_laborer",
    ),
    (
        "construction_underage.md",
        "Construction labourer — knocked out: under 18 (legal requirement)",
        ["hola", "Lucía Pérez", "tengo 17 años"],
        "construction_laborer",
    ),
    (
        "construction_physical_review_en.md",
        "Construction labourer (EN) — reports physical limits: qualified but routed to recruiter review",
        ["Hi, I want to apply", "John Smith", "yes", "yes", "X1234567L", "Malaga", "no, I can't lift heavy loads",
         "yes I have it", "5 years as a bricklayer", "I drive my own car", "next week"],
        "construction_laborer",
    ),
]


def render(name: str, title: str, messages: list[str], profile_id: str | None = None) -> str:
    agent = ScreeningAgent(MockLLM())
    language = "en" if messages and messages[0].lower().startswith("hi") else None
    state = agent.new_session(source="web", profile_id=profile_id, language=language)
    profile = agent.profile(state)
    bot = profile.persona.agent_name
    lines = [f"# {title}", "",
             f"_Profile {profile.id} ({profile.company}) · session {state.id} · language {state.language.value} · "
             f"today {TODAY.isoformat()}_", ""]
    opening = agent.opening(state)
    lines.append(f"**{bot}:** {opening}")
    requested = []
    for msg in messages:
        reply, decision = agent.process(state, msg, today=TODAY)
        requested.extend(decision.check_requests)
        lines.append("")
        lines.append(f"**Candidate:** {msg}")
        lines.append("")
        lines.append(f"**{bot}:** {reply}")
        if decision.rag_used:
            lines.append("")
            lines.append(f"_RAG: answered from FAQ {', '.join(decision.rag_used)}_")
        if decision.stage_before != decision.stage_after:
            lines.append("")
            lines.append(f"_Stage: {decision.stage_before} → {decision.stage_after}_")
    # external checks answer asynchronously (the worker, here the mock registry at once)
    from backend.verifications.providers import MockIdRegistry

    for req in requested:
        status, detail = MockIdRegistry(delay_seconds=0).poll({"due_at": None}, req["payload"], None)
        late, _ = agent.apply_check_result(state, req["check"], status, detail, today=TODAY)
        lines.append("")
        lines.append(f"_Async check {req['check']}: {status} ({detail})_")
        if late:
            lines.append("")
            lines.append(f"**{bot}:** {late}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"**Final stage:** {state.stage} · **flags:** {state.flags or '—'}")
    if state.summary:
        lines.append("")
        lines.append("```json")
        import json
        lines.append(json.dumps(state.summary, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    return "\n".join(lines) + "\n"


def main() -> None:
    for fname, title, messages, *profile in SCENARIOS:
        text = render(fname, title, messages, profile[0] if profile else None)
        with open(f"samples/{fname}", "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote samples/{fname}")


if __name__ == "__main__":
    main()
