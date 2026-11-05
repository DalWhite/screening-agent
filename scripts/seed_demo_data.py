"""Generate a realistic demo dataset (default: 1000 candidates over 90 days).

Use it to try the backoffice (analytics, pipeline, search) with real volume:

    python scripts/seed_demo_data.py --db data/demo.db --reset
    LLM_FALLBACK=mock DB_PATH=data/demo.db python -m uvicorn backend.app:app --port 8001

Nothing is written straight into the tables. Every candidate is screened by the
real engine (MockLLM + deterministic parsing) and every recruiter action goes
through ``BackofficeService``, on a *simulated clock*. As a result, funnels,
drop-off, durations, FAQ usage, external checks, re-engagement nudges, pipeline
moves, notes and the audit timeline are all consistent with each other, as if
the system had been running in production for the whole period.

The mix is deterministic for a given ``--seed``: both seeded processes (the
construction labourer process gets ~60% of the volume, the courier ~40%), ES/EN
candidates, web/WhatsApp/voice/manual channels, knockouts, reviews, abandoned
conversations (with nudges, some recovered), FAQ questions, ID checks (valid,
not found, registry down), manual licence checks, recruiters moving candidates
through shortlisted → interview → offer → hired or rejecting them, owners,
tags, notes, interviews and human takeovers (some still waiting for an answer).

Candidates whose conversation is still running ("En conversación") are placed
in the last few minutes. When you start the server later, the re-engagement
monitor treats them as it would in production: it nudges them and, after
``SESSION_IDLE_SECONDS``, closes them as abandoned.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------------------
# Simulated clock: every "now" in the engine, storage and workers reads it.
# --------------------------------------------------------------------------------------


class SimClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def set(self, value: datetime) -> None:
        self.now = value

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


CLOCK = SimClock()


class _SimDateTime(datetime):
    """`datetime` whose now() is the simulated clock (everything else is unchanged)."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = CLOCK.now
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return CLOCK.now.replace(tzinfo=None)


def _install_clock() -> None:
    from backend import reengagement
    from backend.screening import agent, schema
    from backend.storage import db, profile_store
    from backend.verifications import service

    for module in (agent, schema, db, profile_store, reengagement, service):
        module.datetime = _SimDateTime  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

FIRST_ES_M = ["Carlos", "Javier", "Miguel", "David", "José", "Antonio", "Manuel", "Francisco", "Daniel", "Alejandro",
              "Pablo", "Sergio", "Jorge", "Alberto", "Raúl", "Rubén", "Iván", "Óscar", "Adrián", "Diego", "Hugo",
              "Álvaro", "Mario", "Víctor", "Ismael", "Andrés", "Luis", "Rafael", "Ángel", "Fernando", "Mohamed",
              "Youssef", "Ion", "Mihai", "Kevin", "Brayan", "Jhon", "Wilson", "Ricardo", "Gustavo"]
FIRST_ES_F = ["María", "Laura", "Lucía", "Ana", "Carmen", "Marta", "Paula", "Sara", "Elena", "Cristina", "Raquel",
              "Patricia", "Rocío", "Nuria", "Irene", "Silvia", "Beatriz", "Alba", "Noelia", "Andrea", "Fátima",
              "Yolanda", "Lorena", "Sandra", "Daniela", "Valentina", "Camila", "Gabriela", "Ioana", "Khadija"]
SURNAMES_ES = ["García", "Martínez", "López", "Sánchez", "Pérez", "Gómez", "Martín", "Jiménez", "Ruiz", "Hernández",
               "Díaz", "Moreno", "Muñoz", "Álvarez", "Romero", "Alonso", "Gutiérrez", "Navarro", "Torres", "Domínguez",
               "Vázquez", "Ramos", "Gil", "Ramírez", "Serrano", "Blanco", "Molina", "Morales", "Suárez", "Ortega",
               "Delgado", "Castro", "Ortiz", "Rubio", "Marín", "Sanz", "Núñez", "Iglesias", "Medina", "Garrido",
               "Cortés", "Castillo", "Santos", "Lozano", "Guerrero", "Cano", "Prieto", "Méndez", "Cruz", "Calvo",
               "Gallego", "Vidal", "León", "Márquez", "Herrera", "Peña", "Flores", "Cabrera", "Campos", "Vega",
               "Fuentes", "Carrasco", "Diez", "Caballero", "Reyes", "Nieto", "Aguilar", "Pascual", "Santana",
               "Herrero", "Lorenzo", "Montero", "Hidalgo", "Giménez", "Ibáñez", "Ferrer", "Durán", "Santiago",
               "Benítez", "Mora", "Vicente", "Arias", "Vargas", "Carmona", "Crespo", "Román", "Pastor", "Soto",
               "Sáez", "Velasco", "Moya", "Soler", "Parra", "Esteban", "Bravo", "Gallardo", "Rojas", "Popescu",
               "El Amrani", "Benali", "Etxeberria", "Quispe", "Mendoza"]
FIRST_EN = ["John", "Michael", "David", "James", "Daniel", "Mark", "Paul", "Peter", "Tom", "Chris", "Emma", "Sarah",
            "Laura", "Anna", "Kate", "Olivia", "Grace", "Samuel", "Oliver", "Jack", "Aisha", "Kwame", "Emeka",
            "Tariq", "Bilal", "Ravi", "Priya", "Nadia", "Liam", "Sean"]
SURNAMES_EN = ["Smith", "Johnson", "Brown", "Taylor", "Wilson", "Davies", "Evans", "Thomas", "Roberts", "Walker",
               "Wright", "Thompson", "White", "Hughes", "Edwards", "Green", "Hall", "Wood", "Harris", "Clarke",
               "O'Brien", "Murphy", "Kelly", "Okafor", "Mensah", "Khan", "Ahmed", "Patel", "Singh", "Novak"]

RECRUITERS = ["Marta Ruiz", "Jorge Andrade", "Lucía Ferrer", "Pablo Ortega"]

DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"

# Construction (Constructora Horizonte)
C_AREAS = ["Getafe", "Vallecas", "Villaverde", "Carabanchel", "Leganés", "Móstoles", "Fuenlabrada", "Alcorcón",
           "Parla", "Madrid", "Madrid, Usera", "Torrejón de Ardoz", "Alcalá de Henares", "Toledo", "Illescas",
           "Valencia", "Paterna", "Torrent", "Sevilla", "Sevilla, Dos Hermanas", "Alcalá de Guadaíra", "Málaga",
           "Torremolinos", "Marbella", "Zaragoza", "Utebo"]
C_OUT = ["Ourense", "Vigo", "Lugo", "Cáceres", "Badajoz", "Santander", "Bilbao", "León", "Burgos", "Soria"]
C_EXPERIENCE_ES = ["3 años de peón y algo de albañilería", "5 años de albañil", "2 años de encofrador",
                   "1 año de peón", "ninguna, sería mi primera obra", "0", "8 años de ferrallista",
                   "4 años de pintor", "6 años de albañil y alicatado", "10 años en obra, de peón y encofrador",
                   "un año de ayudante de fontanero", "2 años de pladur", "7 años de solador",
                   "3 años de demoliciones"]
C_EXPERIENCE_EN = ["5 years as a bricklayer", "2 years as a labourer", "none, it would be my first job", "0",
                   "3 years as a painter", "1 year as a carpenter helper", "4 years in formwork"]
C_START_ES = ["el lunes", "mañana", "en 2 semanas", "ya mismo", "el próximo lunes", "en 3 días", "en una semana",
              "en 10 días", "cuanto antes", "desde el lunes"]
C_START_EN = ["next Monday", "tomorrow", "in 2 weeks", "next week", "in 3 days"]

# Courier (Grupo Sazón)
D_AREAS_ES = ["Madrid, Malasaña", "Madrid, Chamberí", "Madrid", "Getafe", "Barcelona, Gràcia", "Barcelona, Sants",
              "Barcelona", "Badalona", "Valencia, Ruzafa", "Valencia", "Sevilla, Triana", "Sevilla", "Zaragoza",
              "Málaga, Teatinos", "Murcia", "Palma", "Bilbao, Deusto", "Alicante", "Córdoba", "Valladolid",
              "Granada", "Vitoria", "Oviedo", "Gijón"]
D_AREAS_MX = ["Ciudad de México, Roma", "Ciudad de México, Condesa", "Guadalajara", "Zapopan", "Monterrey",
              "Puebla", "Querétaro", "Mérida", "Cancún", "Tijuana", "León"]
D_OUT = ["Ourense", "Vigo", "Lugo", "Cáceres", "Huelva", "Jaén", "Teruel", "Cuenca"]
D_AVAIL_ES = ["jornada completa", "media jornada", "fines de semana", "jornada completa y fines de semana", "media jornada"]
D_AVAIL_EN = ["full time", "part time", "weekends", "part time and weekends"]
D_SCHED_ES = ["mañana", "tarde", "noche", "flexible", "me da igual", "tardes"]
D_SCHED_EN = ["morning", "afternoon", "evening", "flexible"]
D_EXP_ES = ["2 años en Glovo", "1 año en Just Eat", "1 año en Uber Eats", "ninguna", "3 años de repartidor",
            "4 años en mensajería", "0"]
D_EXP_EN = ["1 year with Uber Eats", "2 years at Deliveroo", "none", "1 year with Glovo"]

FAQ_ES = {
    "construction_laborer": ["¿Cuánto se cobra?", "¿Qué horario tiene? ¿Se trabaja los sábados?",
                             "¿Dais las botas y el casco?", "¿Qué tipo de contrato es?",
                             "¿Hace falta experiencia?", "¿Hay transporte de empresa?", "¿Cuándo se cobra?",
                             "¿Qué es el curso de PRL? ¿Quién lo paga?", "¿Pueden trabajar mujeres en obra?"],
    "delivery_courier": ["¿Cuánto se paga?", "¿Qué vehículo necesito?", "¿Es un contrato laboral o autónomo?",
                         "¿Qué pasa si tengo un accidente?", "¿Me dais la mochila?", "¿Qué documentos necesito?",
                         "¿Cuánto tarda la selección?"],
}
FAQ_EN = {
    "construction_laborer": ["How much does it pay?", "Do you provide boots and helmet?", "What kind of contract is it?",
                             "Is there company transport?"],
    "delivery_courier": ["How much does it pay?", "What vehicle do I need?", "What happens if I have an accident?"],
}

NOTES = {
    "call": ["Llamada hecha: muy buena disposición, confirma disponibilidad.", "Hablado por teléfono, encaja bien.",
             "Le he llamado: tiene experiencia real, la explica con detalle.", "Conversación breve, parece responsable.",
             "Confirmado por teléfono que puede incorporarse en la fecha indicada."],
    "review_ok": ["Revisado: la limitación física no afecta a tareas de ayudante de acabados.",
                  "Llamado: tiene bono de transporte, puede llegar a obra sin problema.",
                  "Se ha comprobado el documento en persona. Todo correcto.", "Revisado con el jefe de obra: adelante."],
    "review_ko": ["No puede cargar peso y todas las vacantes actuales lo requieren.",
                  "Sin forma de llegar a las obras activas. Descartado por ahora.",
                  "No contesta tras tres llamadas.", "Ya ha encontrado otro trabajo."],
    "interview": ["Entrevista programada en la oficina de obra.", "Citado para entrevista y prueba práctica.",
                  "Entrevista por videollamada confirmada."],
    "interview_done": ["Buena entrevista. Pasa a oferta.", "Muy buena actitud en la prueba práctica.",
                       "Entrevista correcta; encaja con la obra de Valdebebas."],
    "rejected": ["No se presentó a la entrevista.", "Ha aceptado otra oferta.", "No encaja con el turno disponible.",
                 "Sin respuesta tras varias llamadas.", "Pide un salario fuera de banda."],
    "offer": ["Oferta enviada por WhatsApp. Pendiente de firma.", "Oferta aceptada verbalmente."],
    "hired": ["Incorporado. Alta en Seguridad Social tramitada.", "Empieza el lunes en obra."],
    "abandoned": ["No terminó el chat. Le llamo mañana.", "Dejó el chat a medias; le he escrito por WhatsApp."],
}
TAGS = {
    "construction_laborer": ["albañilería", "urgente", "obra Valdebebas", "referido", "repesca", "carnet de conducir",
                             "bilingüe", "con herramientas propias"],
    "delivery_courier": ["moto propia", "turno noche", "urgente", "referido", "repesca", "bilingüe", "bici eléctrica"],
}
TAKEOVER_OPENERS = ["Hola {first}, soy {by} de selección. ¿Te viene bien una llamada mañana a las 10?",
                    "Hola {first}, te escribe {by}. ¿Podrías enviarnos una foto del certificado PRL?",
                    "Hola {first}, soy {by}. ¿Sigues interesado/a en la vacante? Tenemos una entrevista el jueves."]
TAKEOVER_REPLIES = ["Sí, perfecto, me viene bien.", "Vale, ahora mismo la mando.", "Sí, sigo interesado. ¿A qué hora?",
                    "Mañana no puedo, ¿el viernes?", "Claro, gracias!"]
TAKEOVER_CLOSERS = ["Genial, te llamamos entonces. ¡Gracias!", "Perfecto, recibido. Seguimos en contacto.",
                    "Apuntado. Te confirmo la hora por aquí."]


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


# --------------------------------------------------------------------------------------
# Candidate personas
# --------------------------------------------------------------------------------------


@dataclass
class Persona:
    profile_id: str
    lang: str
    source: str
    name: str
    contact: Optional[str]
    answers: dict[str, list[str]]
    expected: str                            # qualified | review | disqualified:<reason> | abandoned | active
    abandon_at: Optional[str] = None         # question where the candidate goes silent
    recover: bool = False                    # comes back after the first nudge
    faq: dict[str, str] = field(default_factory=dict)   # question id -> FAQ asked right before answering it
    greeting: str = "hola"
    manual: bool = False                     # registered by a recruiter (referral / walk-in)
    manual_never_started: bool = False
    pace: float = 30.0                       # median seconds between candidate messages


def dni(rng: random.Random, prefix: str = "") -> str:
    while True:
        number = prefix + "".join(str(rng.randint(0, 9)) for _ in range(8 - len(prefix)))
        if prefix or number[:2] not in ("99", "98", "97"):
            return f"{number}{DNI_LETTERS[int(number) % 23]}"


def nie(rng: random.Random) -> str:
    first = rng.choice("XYZ")
    digits = "".join(str(rng.randint(0, 9)) for _ in range(7))
    while digits[:2] in ("99", "98", "97"):  # mock registry test prefixes (not found / down / slow)
        digits = "".join(str(rng.randint(0, 9)) for _ in range(7))
    return f"{first}{digits}{DNI_LETTERS[int(str('XYZ'.index(first)) + digits) % 23]}"


def pick_name(rng: random.Random, lang: str, used: set[str]) -> str:
    for _ in range(200):
        if lang == "en":
            name = f"{rng.choice(FIRST_EN)} {rng.choice(SURNAMES_EN)}"
        else:
            first = rng.choice(FIRST_ES_F if rng.random() < 0.32 else FIRST_ES_M)
            name = f"{first} {rng.choice(SURNAMES_ES)} {rng.choice(SURNAMES_ES)}"
        if name not in used:
            used.add(name)
            return name
    raise RuntimeError("ran out of names")


def phone(rng: random.Random, country: str = "ES") -> str:
    if country == "MX":
        return "+521" + "".join(str(rng.randint(0, 9)) for _ in range(10))
    return "+34" + rng.choice("67") + "".join(str(rng.randint(0, 9)) for _ in range(8))


def construction_persona(rng: random.Random, used: set[str]) -> Persona:
    en = rng.random() < 0.14
    lang = "en" if en else "es"
    name = pick_name(rng, lang, used)
    a: dict[str, list[str]] = {"name": [name]}
    expected = "qualified"
    age = rng.randint(18, 61)
    if rng.random() < 0.05:
        a["adult"] = ["I'm 17" if en else rng.choice(["tengo 17 años", "no, tengo 16"])]
        expected = "disqualified:underage"
    else:
        a["adult"] = [f"yes, I'm {age}" if en else rng.choice([f"sí, tengo {age} años", "sí", f"tengo {age} años", "claro"])]
    if rng.random() < 0.07:
        a["work_permit"] = ["no, not yet" if en else rng.choice(["no tengo permiso todavía", "no, estoy esperando los papeles"])]
        expected = expected if expected.startswith("disqualified") else "disqualified:no_work_permit"
    else:
        a["work_permit"] = ["yes" if en else rng.choice(["sí", "sí, tengo NIE con permiso", "sí, soy español", "sí, tengo papeles"])]
    roll = rng.random()
    if roll < 0.035:
        doc = dni(rng, "99")
        expected = expected if expected.startswith("disqualified") else "disqualified:id_not_verified"
    elif roll < 0.055:
        doc = dni(rng, "98")
        expected = expected if expected != "qualified" else "review"
    else:
        doc = nie(rng) if rng.random() < 0.45 else dni(rng)
    a["id_document"] = [rng.choice([doc, f"my ID is {doc}"]) if en else rng.choice([doc, f"mi DNI es {doc}", f"es {doc}"])]
    roll = rng.random()
    if roll < 0.07:
        a["location"] = [rng.choice(C_OUT), rng.choice(C_OUT)]
        expected = expected if expected.startswith("disqualified") else "disqualified:out_of_area"
    elif roll < 0.10:
        a["location"] = [rng.choice(C_OUT), rng.choice(C_AREAS)]
    else:
        a["location"] = [rng.choice(C_AREAS)]
    if rng.random() < 0.08:
        a["physical"] = ["no, I can't lift heavy loads" if en else rng.choice(["no puedo cargar tanto peso", "no, tengo problemas de espalda"])]
        expected = expected if expected != "qualified" else "review"
    else:
        a["physical"] = ["yes, no problem" if en else rng.choice(["sí, sin problema", "sí", "claro, estoy acostumbrado"])]
    a["prl"] = [("no" if en else rng.choice(["no, no lo tengo", "no"])) if rng.random() < 0.45
                else ("yes I have it" if en else rng.choice(["sí, tengo el de 20 horas", "sí"]))]
    a["experience"] = [rng.choice(C_EXPERIENCE_EN if en else C_EXPERIENCE_ES)]
    if rng.random() < 0.06:
        a["transport"] = ["I walk" if en else rng.choice(["a pie", "no tengo, me llevan", "andando"])]
        expected = expected if expected != "qualified" else "review"
    else:
        a["transport"] = [rng.choice(["I drive my own car", "by bus", "public transport"]) if en
                          else rng.choice(["tengo coche", "en autobús", "en metro", "tengo moto", "en transporte público", "en cercanías"])]
    a["start_date"] = [rng.choice(C_START_EN if en else C_START_ES)]
    return Persona("construction_laborer", lang, "", name, None, a, expected,
                   greeting=rng.choice(["Hi, I want to apply", "hi"]) if en else rng.choice(["hola", "Hola, busco trabajo en obra", "buenas"]))


def courier_persona(rng: random.Random, used: set[str]) -> Persona:
    en = rng.random() < 0.12
    lang = "en" if en else "es"
    mx = not en and rng.random() < 0.25
    name = pick_name(rng, lang, used)
    a: dict[str, list[str]] = {"name": [name]}
    expected = "qualified"
    if rng.random() < 0.12:
        a["license"] = ["no, I don't have a licence" if en else rng.choice(["no tengo carnet", "no, no tengo licencia"])]
        expected = "disqualified:no_license"
    else:
        a["license"] = ["yes, I have a valid driving license" if en else rng.choice(["sí, tengo carnet", "sí", "sí, el B"])]
    roll = rng.random()
    areas = D_AREAS_MX if mx else D_AREAS_ES
    if roll < 0.07:
        a["location"] = [rng.choice(D_OUT), rng.choice(D_OUT)]
        expected = expected if expected.startswith("disqualified") else "disqualified:out_of_area"
    elif roll < 0.09:
        a["location"] = [rng.choice(D_OUT), rng.choice(areas)]
    else:
        a["location"] = [rng.choice(areas)]
    if rng.random() < 0.03:
        a["availability"] = ["no sé", "depende", "creo que a veces"]  # given up after 3 re-asks: incomplete, to review
        expected = expected if expected != "qualified" else "abandoned"
    else:
        a["availability"] = [rng.choice(D_AVAIL_EN if en else D_AVAIL_ES)]
    a["schedule"] = [rng.choice(D_SCHED_EN if en else D_SCHED_ES)]
    a["experience"] = [rng.choice(D_EXP_EN if en else D_EXP_ES)]
    a["start_date"] = [rng.choice(C_START_EN if en else C_START_ES)]
    p = Persona("delivery_courier", lang, "", name, None, a, expected,
                greeting=rng.choice(["Hi, I'd like to apply as a courier", "hi"]) if en
                else rng.choice(["hola", "Hola, quiero aplicar para repartidor", "buenas tardes"]))
    p.contact = phone(rng, "MX" if mx else "ES")
    return p


ABANDON_WEIGHTS = {
    "construction_laborer": {"name": 3, "adult": 2, "work_permit": 2, "id_document": 9, "location": 3, "physical": 2,
                             "prl": 1, "experience": 2, "transport": 1, "start_date": 1},
    "delivery_courier": {"name": 3, "license": 2, "location": 3, "availability": 3, "schedule": 2, "experience": 2,
                         "start_date": 1},
}


def make_persona(rng: random.Random, used: set[str], profile_id: str) -> Persona:
    p = construction_persona(rng, used) if profile_id == "construction_laborer" else courier_persona(rng, used)
    roll = rng.random()
    p.source = "web" if roll < 0.55 else "whatsapp" if roll < 0.88 else "voice"
    if p.source != "web" and not p.contact:
        p.contact = phone(rng)
    elif p.source == "web" and rng.random() < 0.5:
        p.contact = None
    p.pace = {"web": 25.0, "whatsapp": 70.0, "voice": 8.0}[p.source]
    # about 1 in 6 goes silent at some point; a quarter of them come back after the first nudge
    if not p.expected.startswith("disqualified") and rng.random() < 0.17:
        weights = ABANDON_WEIGHTS[profile_id]
        p.abandon_at = rng.choices(list(weights), list(weights.values()))[0]
        p.recover = rng.random() < 0.25
        if not p.recover:
            p.expected = "abandoned"
    # FAQ questions asked mid-conversation
    if p.source != "voice" and rng.random() < 0.28:
        pool = (FAQ_EN if p.lang == "en" else FAQ_ES)[profile_id]
        questions = [q for q in p.answers if q != "name"]
        for _ in range(1 if rng.random() < 0.75 else 2):
            p.faq[rng.choice(questions)] = rng.choice(pool)
    if rng.random() < 0.025:
        p.manual = True
        p.source = "backoffice"
        p.contact = p.contact or phone(rng)
        p.manual_never_started = rng.random() < 0.3
        if p.abandon_at == "name":  # the recruiter already gave the name: it is never asked
            p.abandon_at = list(p.answers)[1]
    return p


# --------------------------------------------------------------------------------------
# Timeline helpers
# --------------------------------------------------------------------------------------


def pick_start(rng: random.Random, now: datetime, days: int) -> datetime:
    """Recent days are busier (growing campaign); weekends are quieter; evenings peak."""
    while True:
        ago = int(rng.triangular(0, days, 0))
        day = (now - timedelta(days=ago)).date()
        if day.weekday() >= 5 and rng.random() < 0.45:
            continue
        hour = rng.choices(range(7, 24), [1, 3, 5, 6, 6, 5, 4, 4, 4, 4, 5, 6, 7, 7, 6, 4, 2])[0]
        start = datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59), tzinfo=timezone.utc)
        if start < now - timedelta(minutes=30):
            return start


def business_time(rng: random.Random, after: datetime, min_hours: float, max_hours: float) -> datetime:
    """A moment `min..max` hours after `after`, moved into office hours (Mon-Fri, 9:00-19:00 UTC)."""
    t = after + timedelta(hours=rng.uniform(min_hours, max_hours))
    for _ in range(10):
        if t.weekday() >= 5 or t.hour >= 19:
            t = (t + timedelta(days=1)).replace(hour=9, minute=rng.randint(0, 59))
        elif t.hour < 9:
            t = t.replace(hour=9, minute=rng.randint(0, 59))
        else:
            break
    return t


# --------------------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------------------


class Generator:
    def __init__(self, count: int, days: int, seed: int, quiet: bool = False):
        from backend.backoffice.service import BackofficeService
        from backend.channels.base import persist_turn
        from backend.config import settings
        from backend.llm.client import MockLLM
        from backend.reengagement import ReengagementMonitor
        from backend.screening.agent import ScreeningAgent
        from backend.screening.profile import registry
        from backend.session_store import SessionStore
        from backend.storage import db
        from backend.storage.profile_store import ProfileStore
        from backend import verifications
        from backend.verifications.service import VerificationService

        self.rng = random.Random(seed)
        self.count, self.days, self.quiet = count, days, quiet
        self.real_now = datetime.now(timezone.utc).replace(microsecond=0)
        self.settings = settings
        self.db = db
        self.persist_turn = persist_turn
        # the profiles' first revision exists before the first candidate
        CLOCK.set(self.real_now - timedelta(days=days + 2))
        db.init_db()
        profiles = registry()
        profiles.attach_store(ProfileStore())
        self.agent = ScreeningAgent(MockLLM(), profiles)
        self.store = SessionStore()
        # as in production with Twilio configured: WhatsApp messages are pushed, web ones wait for the chat to poll
        push = lambda state, text: state.source == "whatsapp"  # noqa: E731
        self.monitor = ReengagementMonitor(self.agent, {}, notifier=push)
        self.verifications = VerificationService(self.agent, self.store, notifier=push)
        verifications.set_service(self.verifications)
        self.bo = BackofficeService(self.agent, self.store, notifier=push, verifications=self.verifications)
        self.profile_ids = [p for p in ("construction_laborer", "delivery_courier") if p in profiles]
        if not self.profile_ids:
            raise SystemExit("the seeded processes (construction_laborer, delivery_courier) are missing from this database")
        self.used_names: set[str] = set()
        self.stats: Counter[str] = Counter()
        self.mismatches: list[str] = []

    # ---------- conversation ----------

    def wait(self, seconds: float) -> None:
        """Let time pass; the verification worker ticks meanwhile (external checks answer while the
        candidate keeps chatting), like the real worker does every second."""
        remaining = max(1.0, seconds)
        while remaining > 0:
            step = min(remaining, 6.0)
            CLOCK.advance(step)
            remaining -= step
            self.verifications.tick()
            if remaining > 30 and not self.open_checks():
                CLOCK.advance(remaining)
                break

    def open_checks(self) -> bool:
        with self.db.get_conn() as conn:
            return conn.execute("SELECT 1 FROM verifications WHERE status IN ('queued','submitted') "
                                "AND provider != 'manual' LIMIT 1").fetchone() is not None

    def think(self, p: Persona) -> float:
        return self.rng.lognormvariate(0, 0.6) * p.pace

    def say(self, state, text: str) -> None:
        CLOCK.advance(self.rng.uniform(1.5, 4.0))  # the assistant's own latency is included in the turn
        reply, decision = self.agent.process(state, text, today=CLOCK.now.date())
        self.persist_turn(state, text, reply, decision)
        self.verifications.tick()

    def converse(self, state, p: Persona, skip_greeting: bool = False) -> None:
        profile = self.agent.profile(state)
        questions = {q.id for q in profile.questions}
        asked: Counter[str] = Counter()
        if not skip_greeting:
            self.wait(self.rng.uniform(4, 40))
            self.say(state, p.greeting)
        for _ in range(30):
            if state.status != "active" or state.stage not in questions or state.bot_paused:
                break
            qid = state.stage
            if qid == p.abandon_at and asked[qid] == 0:
                if not self.go_silent(state, p):
                    return
                p.abandon_at = None
            if qid in p.faq and asked[qid] == 0:
                self.wait(self.think(p))
                if state.status != "active":
                    break
                self.say(state, p.faq.pop(qid))
                self.stats["faq_asked"] += 1
            answers = p.answers.get(qid) or ["sí"]
            text = answers[min(asked[qid], len(answers) - 1)]
            asked[qid] += 1
            self.wait(self.think(p))
            if state.status != "active":  # e.g. an ID knockout arrived while the candidate was typing
                break
            self.say(state, text)
        # blocking checks still running: the worker answers a few seconds later
        for _ in range(10):
            if state.status != "verifying":
                break
            self.wait(self.rng.uniform(3, 8))

    def go_silent(self, state, p: Persona) -> bool:
        """Nudge 1 (and 2) like the re-engagement monitor; returns True if the candidate comes back."""
        s = self.settings
        self.wait(s.reengage_first_delay + self.rng.uniform(0, 15))
        self.monitor._nudge(state, 1)
        if p.recover:
            self.wait(self.rng.uniform(20, s.reengage_second_delay - s.reengage_first_delay - 5))
            self.stats["recovered_after_nudge"] += 1
            return True
        self.wait(s.reengage_second_delay - s.reengage_first_delay + self.rng.uniform(0, 15))
        self.monitor._nudge(state, 2)
        self.wait(s.session_idle_seconds - s.reengage_second_delay + self.rng.uniform(0, 30))
        self.monitor._close_abandoned(state)
        return False

    # ---------- one candidate ----------

    def candidate(self, start: datetime, profile_id: str, live: bool = False) -> None:
        from backend.screening.schema import Stage

        p = make_persona(self.rng, self.used_names, profile_id)
        if live:
            # still chatting right now: reroll personas that are scripted to knock themselves out
            # or go silent (any prefix of a "qualified" persona's answers is itself knockout-free,
            # by construction) so the partial conversation below can never end in a final decision
            tries = 0
            while p.expected != "qualified" and tries < 30:
                p = make_persona(self.rng, self.used_names, profile_id)
                tries += 1
        CLOCK.set(start)
        if p.manual:
            by = self.rng.choice(RECRUITERS)
            created = self.bo.create_candidate(profile_id, by, name=p.name, contact=p.contact,
                                               language=p.lang, note=self.rng.choice(
                                                   ["Referido por un trabajador de la obra.", "Vino a la oficina a dejar el CV.",
                                                    "Llamó por teléfono preguntando por la oferta.", "Candidato de la bolsa de 2025."]))
            state = self.store.get(created["id"])
            self.stats["manual_created"] += 1
            if p.manual_never_started:
                self.stats["outcome:active"] += 1
                return
            CLOCK.advance(self.rng.uniform(120, 1800))  # the candidate opens the link they were sent
            self.converse(state, p, skip_greeting=False)
        else:
            state = self.agent.new_session(source=p.source, language=p.lang, contact=p.contact, profile_id=profile_id)
            self.store.put(state)
            opening = self.agent.opening(state)
            self.db.save_session(state)
            self.db.append_message(state.id, "assistant", opening, Stage.INTRO)
            if live:
                # answer a few questions and stop; never the one that triggers a *blocking* check
                # (e.g. the ID registry): its async result would otherwise keep resolving in the
                # background (the next candidates' clock keeps ticking the verification worker) and
                # flip this "still active" one to a final decision before the script even finishes
                profile = self.agent.profile(state)
                order = [q.id for q in profile.questions]
                blocking_triggers = {c.trigger for c in profile.checks if c.blocking}
                safe = next((i for i, q in enumerate(order) if q in blocking_triggers), len(order))
                keep = self.rng.randint(1, max(1, min(5, safe)))
                p.abandon_at = None
                p.answers = {k: v for k, v in p.answers.items() if k in order[:keep]}
                p.answers[order[keep]] = []  # stops before answering this one
                self.converse_partial(state, p, order[keep])
                self.stats["outcome:active"] += 1
                return
            self.converse(state, p)
        self.check_expectation(state, p)
        self.recruiter_follow_up(state, p)

    def converse_partial(self, state, p: Persona, stop_at: str) -> None:
        self.wait(self.rng.uniform(4, 30))
        self.say(state, p.greeting)
        for _ in range(20):
            if state.status != "active" or state.stage == stop_at:
                return
            answers = p.answers.get(state.stage)
            if not answers:
                return
            self.wait(self.think(p))
            self.say(state, answers[0])

    def check_expectation(self, state, p: Persona) -> None:
        outcome = state.status
        if outcome == "disqualified":
            outcome = f"disqualified:{state.disqualify_reason}"
        elif outcome == "qualified" and (
                any(f in state.flags for f in ("physical_review", "transport_review", "needs_review"))
                or any(f.startswith("check_error") for f in state.flags)):
            outcome = "review"
        self.stats[f"outcome:{state.status}"] += 1
        if p.expected != outcome and not (p.expected.startswith("disqualified") and outcome.startswith("disqualified")):
            self.mismatches.append(f"{p.profile_id} {p.lang} expected {p.expected} got {outcome} ({state.id})")

    # ---------- recruiters ----------

    def at(self, t: datetime) -> bool:
        """Move the clock to t if it is in the past (recruiters can't act in the future)."""
        if t >= self.real_now - timedelta(minutes=10):
            return False
        CLOCK.set(max(t, CLOCK.now))
        self.verifications.tick()
        return True

    def recruiter_follow_up(self, state, p: Persona) -> None:
        rng, bo = self.rng, self.bo
        sid = state.id
        ended = CLOCK.now
        session = self.db.get_session(sid)
        stage = session["pipeline_stage"]
        by = rng.choice(RECRUITERS)

        # manual licence checks (courier): decided by a recruiter within a few days
        for v in self.verifications.for_session(sid):
            if v["provider"] == "manual" and v["status"] in ("queued", "submitted"):
                if self.at(business_time(rng, ended, 2, 72)):
                    ok = rng.random() < 0.9
                    bo.decide_verification(sid, v["id"], "valid" if ok else "invalid",
                                           "Carnet comprobado por foto" if ok else "El carnet está caducado", by)
                    self.stats["manual_checks_decided"] += 1
            elif v["status"] == "error" and v["provider"] == "mock_id_registry":
                if self.at(business_time(rng, ended, 1, 48)):
                    bo.decide_verification(sid, v["id"], "valid", "DNI comprobado en persona en la oficina", by)
                    self.stats["id_checks_resolved_by_hand"] += 1

        first_touch = business_time(rng, ended, 0.5, 40)
        if not self.at(first_touch):
            return  # too recent: nobody has looked at it yet
        session = self.db.get_session(sid)
        stage = session["pipeline_stage"]
        if state.status in ("qualified", "abandoned", "verifying") or stage in ("review", "qualified"):
            if rng.random() < 0.75:
                bo.update(sid, by, assignee=by)
            if rng.random() < 0.18:
                tags = rng.sample(TAGS[p.profile_id], rng.choice([1, 1, 2]))
                bo.update(sid, by, tags=tags)

        if stage == "review":
            if state.status == "abandoned":
                roll = rng.random()
                if roll < 0.35:
                    bo.add_note(sid, rng.choice(NOTES["abandoned"]), by)
                if roll < 0.6 and self.at(business_time(rng, CLOCK.now, 24, 120)):
                    bo.move(sid, "rejected", by)
                    bo.add_note(sid, "Sin respuesta tras varias llamadas.", by)
                return
            roll = rng.random()
            if roll < 0.45:
                bo.add_note(sid, rng.choice(NOTES["review_ok"]), by)
                bo.move(sid, "qualified", by)
                self.progress(sid, p, by)
            elif roll < 0.75:
                bo.add_note(sid, rng.choice(NOTES["review_ko"]), by)
                bo.move(sid, "rejected", by)
            return
        if stage == "qualified":
            if rng.random() < 0.05:
                self.takeover(state, p, by)
            self.progress(sid, p, by)

    def progress(self, sid: str, p: Persona, by: str) -> None:
        """qualified → shortlisted → interview → offer → hired, with drop-outs at every step."""
        rng, bo = self.rng, self.bo
        steps = [("shortlisted", 0.68, 2, 72), ("interview", 0.72, 4, 96), ("offer", 0.5, 24, 120), ("hired", 0.78, 24, 120)]
        for stage, p_next, lo, hi in steps:
            when = business_time(rng, CLOCK.now, lo, hi)
            if rng.random() > p_next:
                if rng.random() < 0.55 and self.at(when):
                    bo.move(sid, "rejected", by)
                    bo.add_note(sid, rng.choice(NOTES["rejected"]), by)
                return
            if not self.at(when):
                return
            if stage == "shortlisted" and rng.random() < 0.5:
                bo.add_note(sid, rng.choice(NOTES["call"]), by)
            if stage == "interview":
                slot = business_time(rng, CLOCK.now, 20, 96).replace(minute=rng.choice([0, 30]), second=0)
                bo.update(sid, by, pipeline_stage="interview", interview_at=slot.replace(tzinfo=None).isoformat(timespec="minutes"))
                bo.add_note(sid, rng.choice(NOTES["interview"]), by)
                if not self.at(slot + timedelta(hours=2)):
                    return
                continue
            bo.move(sid, stage, by)
            if stage == "offer":
                bo.add_note(sid, rng.choice(NOTES["interview_done"]) + " " + rng.choice(NOTES["offer"]), by)
            if stage == "hired":
                bo.add_note(sid, rng.choice(NOTES["hired"]), by)
                self.stats["hired"] += 1

    def takeover(self, state, p: Persona, by: str) -> None:
        rng = self.rng
        first = p.name.split()[0]
        self.bo.send_message(state.id, rng.choice(TAKEOVER_OPENERS).format(first=first, by=by.split()[0]), by)
        self.stats["takeovers"] += 1
        reply_at = CLOCK.now + timedelta(minutes=rng.uniform(3, 240))
        if not self.at(reply_at):
            return
        self.say(state, rng.choice(TAKEOVER_REPLIES))
        answer_at = business_time(rng, CLOCK.now, 0.2, 20)
        if rng.random() < 0.2 or not self.at(answer_at):
            self.stats["waiting_for_recruiter"] += 1  # the candidate's reply waits for the recruiter
            return
        self.bo.send_message(state.id, rng.choice(TAKEOVER_CLOSERS), by)
        self.bo.takeover(state.id, False, by)

    # ---------- run ----------

    def run(self) -> None:
        started = time.time()
        live = max(1, round(self.count * 0.015))
        plan = []
        for _ in range(self.count - live):
            profile_id = self.rng.choices(self.profile_ids, [0.6, 0.4][: len(self.profile_ids)])[0]
            plan.append((pick_start(self.rng, self.real_now, self.days), profile_id, False))
        for _ in range(live):
            profile_id = self.rng.choice(self.profile_ids)
            plan.append((self.real_now - timedelta(minutes=self.rng.uniform(12, 40)), profile_id, True))
        plan.sort(key=lambda x: x[0])  # chronological: workers see the world in order
        for i, (start, profile_id, is_live) in enumerate(plan, 1):
            self.candidate(start, profile_id, live=is_live)
            if not self.quiet and (i % 100 == 0 or i == len(plan)):
                print(f"  {i}/{len(plan)} candidates ({time.time() - started:.0f}s)", flush=True)
        CLOCK.set(self.real_now)
        # the web chat fetched every queued message that the candidate answered afterwards
        with self.db.get_conn() as conn:
            conn.execute("UPDATE messages SET delivered=1 WHERE delivered=0 AND role!='user' AND EXISTS ("
                         "SELECT 1 FROM messages later WHERE later.session_id=messages.session_id "
                         "AND later.role='user' AND later.id>messages.id)")

    def report(self) -> None:
        from backend.analytics import metrics

        a = metrics.compute(self.db.list_sessions(limit=50000))
        print(f"\nCandidates: {a['total_sessions']}  ·  in conversation {a['active_sessions']}  ·  "
              f"pre-qualified {a['qualified_count']} ({a['qualified_rate_pct']}%)  ·  knockouts {a['disqualified_count']}  ·  "
              f"incomplete {a['abandoned_count']}")
        print("By process: " + ", ".join(f"{k} {v}" for k, v in a["by_profile"].items()))
        print("Pipeline:   " + ", ".join(f"{k} {v}" for k, v in a["pipeline"].items()))
        print("Knockouts:  " + ", ".join(f"{k} {v}" for k, v in a["disqualify_reasons"].items()))
        print("Drop-off:   " + ", ".join(f"{k} {v}" for k, v in a["dropoff_by_stage"].items()))
        print("Languages:  " + ", ".join(f"{k} {v}" for k, v in a["languages"].items()))
        extra = {k: v for k, v in self.stats.items() if not k.startswith("outcome:")}
        print("Activity:   " + ", ".join(f"{k} {v}" for k, v in sorted(extra.items())))
        if self.mismatches:
            print(f"\n{len(self.mismatches)} candidates ended differently than their script intended "
                  f"(the engine decides; run with --verbose to list them)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/demo.db"),
                        help="SQLite file to fill (default: $DB_PATH or data/demo.db)")
    parser.add_argument("--count", type=int, default=1000, help="number of candidates (default 1000)")
    parser.add_argument("--days", type=int, default=90, help="spread them over the last N days (default 90)")
    parser.add_argument("--seed", type=int, default=42, help="random seed: same seed, same mix of candidates (relative to today)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--reset", action="store_true", help="delete the database file first")
    group.add_argument("--append", action="store_true", help="add to a database that already has candidates")
    parser.add_argument("--verbose", action="store_true", help="list candidates whose outcome differs from the script")
    parser.add_argument("--quiet", action="store_true", help="no progress or summary output")
    args = parser.parse_args(argv)

    if args.count < 1 or args.days < 1:
        parser.error("--count and --days must be positive")
    path = Path(args.db)
    if not path.is_absolute():
        path = ROOT / path
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    elif path.exists() and not args.append:
        import sqlite3

        with sqlite3.connect(path) as conn:
            try:
                existing = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            except sqlite3.OperationalError:
                existing = 0
        if existing:
            print(f"{path} already has {existing} candidates. Use --reset to start over or --append to add more.",
                  file=sys.stderr)
            return 2

    # configure the app before importing it: this database, no network, no real LLM
    os.environ["DB_PATH"] = str(path)
    os.environ.setdefault("LLM_FALLBACK", "mock")
    import logging

    logging.disable(logging.WARNING)
    _install_clock()

    if not args.quiet:
        print(f"Generating {args.count} candidates over {args.days} days into {path} (seed {args.seed})…")
    gen = Generator(args.count, args.days, args.seed, quiet=args.quiet)
    gen.run()
    if not args.quiet:
        gen.report()
        if args.verbose:
            for line in gen.mismatches:
                print("  " + line)
        print(f"\nDone. Start the backoffice on it:\n  LLM_FALLBACK=mock DB_PATH={args.db} python -m uvicorn backend.app:app --port 8001\n"
              f"  → http://127.0.0.1:8001/backoffice#/analytics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
