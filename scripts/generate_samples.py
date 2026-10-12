import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm.client import MockLLM
from backend.screening.agent import ScreeningAgent
from backend.screening.schema import Stage

TODAY = date(2026, 10, 11)

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
]


def render(name: str, title: str, messages: list[str]) -> str:
    agent = ScreeningAgent(MockLLM())
    state = agent.new_session(source="chat")
    lines = [f"# {title}", "", f"_Session {state.id} · language {state.language.value} · today {TODAY.isoformat()}_", ""]
    opening = agent.opening(state)
    lines.append(f"**Marisol:** {opening}")
    for msg in messages:
        reply, decision = agent.process(state, msg, today=TODAY)
        lines.append("")
        lines.append(f"**Candidate:** {msg}")
        lines.append("")
        lines.append(f"**Marisol:** {reply}")
        if decision.rag_used:
            lines.append("")
            lines.append(f"_RAG: answered from FAQ {', '.join(decision.rag_used)}_")
        if decision.stage_before is not decision.stage_after:
            lines.append("")
            lines.append(f"_Stage: {decision.stage_before.value} → {decision.stage_after.value}_")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"**Final stage:** {state.stage.value} · **flags:** {state.flags or '—'}")
    if state.summary:
        lines.append("")
        lines.append("```json")
        import json
        lines.append(json.dumps(state.summary, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    return "\n".join(lines) + "\n"


def main() -> None:
    for fname, title, messages in SCENARIOS:
        text = render(fname, title, messages)
        with open(f"samples/{fname}", "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote samples/{fname}")


if __name__ == "__main__":
    main()
