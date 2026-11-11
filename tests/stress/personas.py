"""Varied, randomised candidate scripts for the load test.

Deliberately independent from ``scripts/seed_demo_data.py`` (which drives the
engine in-process on a simulated clock): here every candidate is a plain list
of chat messages sent over real HTTP to a live server, so the only thing that
matters is organic variety — the same mix of knockouts, reviews, FAQ
questions, bilingual candidates and drop-offs a recruiter would actually see,
generated fast enough to launch 100+ of them at once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"

FIRST = ["Carlos", "Laura", "Ahmed", "Lucía", "David", "Fatima", "Jorge", "Nadia", "Iker", "Paula", "Youssef",
         "Marta", "Bilal", "Elena", "Raúl", "Sara", "Mohamed", "Irene", "Kevin", "Noelia"]
SURNAME = ["García", "Pérez", "Martín", "Ruiz", "Gómez", "Benali", "Etxeberria", "Núñez", "Castro", "Moreno",
           "Vidal", "Quispe", "Santana", "Ferrer", "Romero"]
FIRST_EN = ["John", "Emma", "Michael", "Grace", "Tariq", "Olivia", "Samuel", "Priya"]
SURNAME_EN = ["Smith", "Brown", "Taylor", "Khan", "Wilson", "O'Brien"]

C_AREAS = ["Getafe", "Madrid", "Toledo", "Valencia", "Sevilla", "Málaga", "Zaragoza", "Alcalá de Henares"]
C_OUT = ["Ourense", "Vigo", "Lugo", "Cáceres"]
C_EXPERIENCE = ["3 años de peón", "ninguna, sería mi primera obra", "5 años de albañil", "2 años de encofrador",
               "8 años de ferrallista", "0"]
D_AREAS = ["Madrid, Malasaña", "Barcelona, Gràcia", "Valencia", "Sevilla", "Zaragoza", "Bilbao"]
D_EXPERIENCE = ["2 años en Glovo", "ninguna", "1 año en Just Eat", "3 años de repartidor"]
STARTS = ["el lunes", "mañana", "en 2 semanas", "ya mismo", "la próxima semana"]
FAQ_C = ["¿Cuánto se cobra?", "¿Dais las botas y el casco?", "¿Qué es el curso de PRL?"]
FAQ_D = ["¿Cuánto se paga?", "¿Qué vehículo necesito?", "¿Es contrato o autónomo?"]


def _dni(rng: random.Random, bad: str = "") -> str:
    digits = bad + "".join(str(rng.randint(0, 9)) for _ in range(8 - len(bad)))
    return f"{digits}{DNI_LETTERS[int(digits) % 23]}"


def _name(rng: random.Random, en: bool) -> str:
    # candidates don't need unique names (this is a private, throwaway database): two "Carlos
    # Pérez" in the same run is realistic, not a bug
    first, surname = (FIRST_EN, SURNAME_EN) if en else (FIRST, SURNAME)
    return f"{rng.choice(first)} {rng.choice(surname)} {rng.choice(surname)}"


CONSTRUCTION_SCENARIOS = ("qualified", "qualified_flag", "knockout", "out_of_area", "id_unknown", "id_error",
                         "review_given_up", "abandon")
CONSTRUCTION_WEIGHTS = (30, 16, 10, 8, 7, 4, 8, 17)
COURIER_SCENARIOS = ("qualified", "knockout", "review_given_up", "abandon")  # no physical/PRL: no "flag" outcome
COURIER_WEIGHTS = (45, 18, 14, 23)


@dataclass
class Plan:
    label: str
    profile_id: str
    lang: str
    messages: list[str]
    scenario: str
    truncated: bool = False          # "abandon": stopped sending on purpose
    expect_status: str = "qualified"  # qualified | disqualified | abandoned | active
    faq_asked: bool = False
    tags: dict = field(default_factory=dict)


def construction_plan(rng: random.Random, i: int) -> Plan:
    en = rng.random() < 0.15
    name = _name(rng, en)
    scenario = rng.choices(CONSTRUCTION_SCENARIOS, weights=CONSTRUCTION_WEIGHTS)[0]
    msgs = ["Hi, I want to apply" if en else "Hola, busco trabajo en obra", name]

    if scenario == "knockout" and rng.random() < 0.5:  # underage: the fastest possible knockout
        msgs.append("I'm 16" if en else "tengo 16 años")
        return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, "knockout_underage",
                   expect_status="disqualified")

    msgs.append("yes, I'm 28" if en else "sí, tengo 28 años")
    if scenario == "knockout":
        msgs.append("no, not yet" if en else "no tengo permiso todavía")
        return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, "knockout_permit",
                   expect_status="disqualified")
    msgs.append("yes" if en else "sí, tengo NIE con permiso")

    doc = _dni(rng, "99" if scenario == "id_unknown" else "98" if scenario == "id_error" else "")
    msgs.append(f"my ID is {doc}" if en else f"mi DNI es {doc}")

    if scenario == "out_of_area":
        msgs += [rng.choice(C_OUT), rng.choice(C_OUT)]  # still outside after the one retry: knockout
        return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, "knockout_area",
                   expect_status="disqualified")
    msgs.append(rng.choice(C_AREAS))

    faq_asked = rng.random() < 0.3
    if faq_asked:
        msgs.append(rng.choice(FAQ_C))
    msgs.append("yes, no problem" if en else "sí, sin problema")
    msgs.append(("no" if scenario == "qualified_flag" else "yes I have it") if en
                else ("no, no lo tengo" if scenario == "qualified_flag" else "sí"))

    if scenario == "review_given_up":
        # 3 re-asks are accepted as incomplete (a "give_up_field" flag) and the engine moves on to the
        # next question; nothing answers it, so — like "abandon" — it only closes once idle long enough
        msgs += ["no sé", "depende", "creo que a veces"]
        return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, scenario,
                   truncated=True, expect_status="active", faq_asked=faq_asked)

    msgs.append(rng.choice(C_EXPERIENCE))
    msgs.append("by bus" if en else "tengo coche")
    msgs.append(rng.choice(STARTS))
    if scenario == "abandon":
        msgs = msgs[: rng.randint(4, len(msgs) - 1)]
        return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, scenario,
                   truncated=True, expect_status="active", faq_asked=faq_asked)
    # id_unknown: the registry answers mid-conversation (or right after) that the document doesn't
    # exist — a knockout like any other, just discovered asynchronously instead of on the spot
    expect = "disqualified" if scenario == "id_unknown" else "qualified"
    return Plan(f"c{i}", "construction_laborer", "en" if en else "es", msgs, scenario,
               expect_status=expect, faq_asked=faq_asked)


def courier_plan(rng: random.Random, i: int) -> Plan:
    en = rng.random() < 0.15
    name = _name(rng, en)
    scenario = rng.choices(COURIER_SCENARIOS, weights=COURIER_WEIGHTS)[0]
    lang = "en" if en else "es"
    msgs = ["Hi, I'd like to apply as a courier" if en else "Hola, quiero aplicar para repartidor", name]

    if scenario == "knockout":
        msgs.append("no, I don't have one" if en else "no tengo carnet")
        return Plan(f"c{i}", "delivery_courier", lang, msgs, scenario, expect_status="disqualified")

    msgs.append("yes, I have a licence" if en else "sí, tengo carnet")
    msgs.append(rng.choice(D_AREAS))
    faq_asked = rng.random() < 0.25
    if faq_asked:
        msgs.append(rng.choice(FAQ_D))

    if scenario == "review_given_up":
        msgs += ["no sé", "depende", "creo que a veces"]
        return Plan(f"c{i}", "delivery_courier", lang, msgs, scenario, truncated=True, expect_status="active",
                   faq_asked=faq_asked)

    msgs.append("full time" if en else "jornada completa")
    msgs.append("flexible")
    msgs.append(rng.choice(D_EXPERIENCE))
    msgs.append(rng.choice(STARTS))
    if scenario == "abandon":
        msgs = msgs[: rng.randint(3, len(msgs) - 1)]
        return Plan(f"c{i}", "delivery_courier", lang, msgs, scenario, truncated=True, expect_status="active",
                   faq_asked=faq_asked)
    return Plan(f"c{i}", "delivery_courier", lang, msgs, scenario, expect_status="qualified", faq_asked=faq_asked)


def candidate_plan(rng: random.Random, i: int, construction_ratio: float = 0.55) -> Plan:
    """One organic, randomised candidate out of more than a dozen scenario shapes."""
    plan = construction_plan(rng, i) if rng.random() < construction_ratio else courier_plan(rng, i)
    plan.label = f"c{i}"
    return plan
