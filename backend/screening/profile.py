"""Job screening profiles: the business rules of one recruiting process.

Everything that varies between recruiting processes lives in a declarative
profile:

- persona (agent name, company, tone) and candidate-facing messages
- ordered questions, each bound to a typed field handler (``fields.py``)
- business rules: knockouts (``disqualify``) and recruiter ``flag`` rules
- external checks: asynchronous validations (ID registry, webhooks, manual)
  whose results land in a data field that rules can act on
- scoring rules used to rank candidates in the backoffice
- knowledge: FAQ items (RAG) and coverage areas

Profiles are *data*. They are seeded from ``backend/data/profiles/<id>/`` and,
once the app attaches the profile store, edited in the backoffice as drafts
and published as immutable **revisions**. Every session is pinned to the
revision that screened it, so decisions stay auditable and an edit never
changes the rules of a conversation in flight.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from ..config import settings
from .fields import FieldContext, handler_for
from .rag import FaqIndex
from .schema import Stage
from .service_areas import ServiceAreas

log = logging.getLogger("sazon.profiles")

LocalText = dict[str, str]
LocalVariants = dict[str, list[str]]

LIFECYCLE_IDS = {Stage.INTRO, Stage.REVIEW, Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED, Stage.VERIFYING}
ON_COMPLETE = "@complete"
CHECK_RESULTS = ("pending", "valid", "invalid", "error")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

STRICT = ConfigDict(extra="forbid")


def localize(text: Optional[dict[str, Any]], lang: str, default: Any = "") -> Any:
    """Pick ``text[lang]``, falling back to Spanish, English, then any value."""
    if not text:
        return default
    for key in (lang, "es", "en"):
        if key in text and text[key]:
            return text[key]
    return next(iter(text.values()), default)


class ChoiceOption(BaseModel):
    model_config = STRICT
    value: str
    label: LocalText
    keywords: list[str] = Field(default_factory=list)


QuestionType = Literal["name", "yes_no", "choice", "multi_choice", "number", "experience", "date", "location", "text",
                       "id_document"]


class QuestionSpec(BaseModel):
    """One screening question. ``id`` is the conversation stage, ``field`` the data key."""

    model_config = STRICT

    id: str
    field: str
    type: QuestionType
    required: bool = True
    label: LocalText
    description: str = ""
    prompts: LocalVariants
    reask: dict[str, LocalText] = Field(default_factory=dict)
    options: list[ChoiceOption] = Field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    number_threshold: Optional[int] = None  # yes_no: "tengo 17" -> False when threshold is 18
    max_days_ahead: int = 180               # date: acceptable start window
    tags: dict[str, list[str]] = Field(default_factory=dict)  # experience: canonical tag -> aliases
    tags_field: Optional[str] = None
    zone_field: str = "zone"                # location auxiliary keys
    country_field: str = "country"
    coverage_field: str = "in_service_area"
    documents: list[Literal["dni", "nie", "passport"]] = Field(default_factory=lambda: ["dni", "nie"])
    doc_type_field: Optional[str] = None    # id_document: where the document type is stored

    @model_validator(mode="after")
    def _check(self) -> "QuestionSpec":
        if not _SLUG.match(self.id):
            raise ValueError(f"id «{self.id}» no válido: minúsculas, números y _")
        if not _SLUG.match(self.field):
            raise ValueError(f"campo «{self.field}» no válido: minúsculas, números y _")
        if self.type in ("choice", "multi_choice") and not self.options:
            raise ValueError(f"la pregunta «{self.id}» es de opciones y no tiene ninguna")
        if not any(v for variants in self.prompts.values() for v in variants if str(v).strip()):
            raise ValueError(f"la pregunta «{self.id}» no tiene texto")
        if self.id in LIFECYCLE_IDS:
            raise ValueError(f"el id «{self.id}» está reservado")
        values = [o.value for o in self.options]
        if len(values) != len(set(values)):
            raise ValueError(f"la pregunta «{self.id}» tiene opciones repetidas")
        return self

    @property
    def handler(self):
        return handler_for(self)

    @property
    def type_field(self) -> str:
        return self.doc_type_field or f"{self.field}_type"

    def keys(self) -> list[str]:
        return self.handler.keys(self)

    def option_label(self, value: str, lang: str) -> str:
        for option in self.options:
            if option.value == value:
                return localize(option.label, lang, value)
        return value

    def options_text(self, lang: str) -> str:
        return ", ".join(localize(o.label, lang, o.value) for o in self.options)


_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "contains", "not_contains", "missing", "present",
        "within_days")


def evaluate(op: str, actual: Any, expected: Any, today: Optional[date] = None) -> bool:
    """Evaluate one rule condition. Missing values only match ``missing``."""
    if op == "missing":
        return actual in (None, [], "")
    if op == "present":
        return actual not in (None, [], "")
    if actual in (None, [], ""):
        return False
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "contains":
            return expected in actual
        if op == "not_contains":
            return expected not in actual
        if op == "within_days":
            start = date.fromisoformat(str(actual)[:10])
            return start <= (today or date.today()) + timedelta(days=int(expected))
    except (TypeError, ValueError):
        return False
    raise ValueError(f"unknown operator {op!r}")


class Condition(BaseModel):
    model_config = STRICT
    field: str
    op: str = "eq"
    value: Any = None

    def matches(self, data: dict[str, Any], today: Optional[date] = None) -> bool:
        return evaluate(self.op, data.get(self.field), self.value, today)


class Rule(BaseModel):
    """A business rule evaluated after every candidate turn and every external check result.

    Two trigger kinds:
    - field condition: ``field`` + ``op`` + ``value`` on the collected data
      (e.g. ``has_license eq false`` or ``id_check eq invalid``)
    - validation error: ``on_error`` raised by ``question`` after
      ``after_attempts`` previous failed attempts (e.g. out of service area twice)

    ``disqualify`` closes the screening with ``message``; ``flag`` only marks
    the candidate for recruiter attention (``flags`` + ``note``).
    """

    model_config = STRICT

    id: str
    action: Literal["disqualify", "flag"] = "disqualify"
    label: LocalText
    field: Optional[str] = None
    op: str = "eq"
    value: Any = None
    on_error: Optional[str] = None
    question: Optional[str] = None
    after_attempts: int = 0
    flags: list[str] = Field(default_factory=list)
    message: Optional[LocalText] = None
    note: Optional[str] = None
    review: bool = False  # flag rules: route an otherwise qualified candidate to recruiter review

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if not _SLUG.match(self.id):
            raise ValueError(f"id de regla «{self.id}» no válido: minúsculas, números y _")
        if bool(self.field) == bool(self.on_error):
            raise ValueError(f"la regla «{self.id}» necesita un dato o un error como disparador (solo uno)")
        if self.on_error and not self.question:
            raise ValueError(f"la regla «{self.id}» se dispara por error y necesita una pregunta")
        if self.op not in _OPS:
            raise ValueError(f"la regla «{self.id}» usa un operador desconocido «{self.op}»")
        return self

    def matches(self, data: dict[str, Any], today: Optional[date] = None) -> bool:
        if not self.field:
            return False
        return evaluate(self.op, data.get(self.field), self.value, today)


class ScoringRule(BaseModel):
    """Points towards the ranking score (0-100, normalised by the profile maximum).

    Either a condition (``op``/``value``) awarding ``points``, or ``per_unit``
    points per unit of a number (or per item of a list), capped by ``max_points``.
    """

    model_config = STRICT

    field: str
    label: LocalText
    op: Optional[str] = None
    value: Any = None
    points: float = 0
    per_unit: Optional[float] = None
    max_points: Optional[float] = None

    @model_validator(mode="after")
    def _check(self) -> "ScoringRule":
        if self.per_unit is None and not self.op:
            raise ValueError("un criterio necesita una condición o puntos por unidad")
        if self.op and self.op not in _OPS:
            raise ValueError(f"operador desconocido «{self.op}»")
        return self

    def ceiling(self) -> float:
        if self.per_unit is not None:
            return float(self.max_points or 0)
        return float(self.points)

    def award(self, data: dict[str, Any], today: Optional[date] = None) -> float:
        actual = data.get(self.field)
        if self.per_unit is not None:
            if actual in (None, [], ""):
                return 0.0
            units = len(actual) if isinstance(actual, list) else float(actual) if not isinstance(actual, bool) else 0
            pts = float(units) * self.per_unit
            return min(pts, float(self.max_points)) if self.max_points is not None else pts
        if self.op and evaluate(self.op, actual, self.value, today):
            return float(self.points)
        return 0.0


class CheckSpec(BaseModel):
    """An external, asynchronous validation (e.g. an ID registry lookup).

    When ``trigger`` (a question id, or ``@complete`` at the end of the
    questions) is answered — and ``when`` holds — the check is queued with the
    ``inputs`` as payload. Its result (``pending`` → ``valid`` | ``invalid`` |
    ``error``) is written into the data field ``result_field`` (default: the
    check id) and the profile's rules are evaluated again, so a rule such as
    ``id_check eq invalid → disqualify`` acts on it. ``blocking`` checks hold
    the final decision: the screening ends in ``verifying`` until they resolve.
    """

    model_config = STRICT

    id: str
    label: LocalText
    provider: Literal["mock_id_registry", "webhook", "manual"] = "manual"
    trigger: str = ON_COMPLETE
    inputs: list[str] = Field(default_factory=list)
    when: Optional[Condition] = None
    blocking: bool = True
    timeout_seconds: int = Field(default=3600, ge=1)
    url: Optional[str] = None
    result_field: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "CheckSpec":
        if not _SLUG.match(self.id):
            raise ValueError(f"id de verificación «{self.id}» no válido: minúsculas, números y _")
        return self

    @property
    def field(self) -> str:
        return self.result_field or self.id


class Persona(BaseModel):
    model_config = STRICT
    agent_name: str
    personality: str
    context: str = ""


class ProfileMessages(BaseModel):
    model_config = STRICT
    qualified: LocalText
    review: LocalText
    resume: LocalText = Field(default_factory=lambda: {
        "es": "¡Bienvenido de nuevo! Retomamos justo donde lo dejaste.",
        "en": "Welcome back! We pick up right where we stopped.",
    })
    reengage: dict[str, LocalText] = Field(default_factory=dict)
    handback: LocalText = Field(default_factory=lambda: {
        "es": "Gracias por tu paciencia. Sigo yo con las preguntas.",
        "en": "Thanks for your patience. I'll carry on with the questions.",
    })
    paused: LocalText = Field(default_factory=lambda: {
        "es": "Un reclutador está revisando tu conversación y te responderá por aquí.",
        "en": "A recruiter is reviewing your conversation and will reply here.",
    })
    verifying: LocalText = Field(default_factory=lambda: {
        "es": "¡Gracias, ya tengo todo! Estoy verificando tus datos y te aviso por aquí en cuanto tenga la respuesta.",
        "en": "Thanks, that's everything! I'm verifying your details and will let you know here as soon as I hear back.",
    })


class FaqItem(BaseModel):
    model_config = STRICT
    id: str
    lang: str = "es"
    topic: str = ""
    q: str
    a: str


class AreaSpec(BaseModel):
    model_config = STRICT
    city: str
    country: str = ""
    aliases: list[str] = Field(default_factory=list)
    zones: list[str] = Field(default_factory=list)


class JobProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", ignored_types=(cached_property,))

    id: str
    version: str = "1"
    active: bool = True
    company: str
    role: LocalText
    summary: LocalText = Field(default_factory=dict)
    sector: str = ""
    persona: Persona
    languages: list[str] = Field(default_factory=lambda: ["es", "en"])
    default_language: str = "es"
    intro: LocalVariants
    messages: ProfileMessages
    questions: list[QuestionSpec]
    rules: list[Rule] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    scoring: list[ScoringRule] = Field(default_factory=list)
    faq: list[FaqItem] = Field(default_factory=list)
    service_areas: list[AreaSpec] = Field(default_factory=list)
    max_stage_attempts: int = Field(default=3, ge=1, le=10)

    # published revision this object represents (not part of the stored JSON)
    revision: int = Field(default=1, exclude=True)

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo) -> "JobProfile":
        if info.context and info.context.get("collect_problems"):
            return self  # the editor collects every problem instead of failing on the first
        problems = cross_problems(self)
        if problems:
            raise ValueError(f"perfil «{self.id}»: " + "; ".join(p["message"] for p in problems))
        return self

    # ---------- structure ----------

    def question_keys(self) -> list[str]:
        keys: list[str] = []
        for q in self.questions:
            for key in q.keys():
                if key not in keys:
                    keys.append(key)
        return keys

    def data_keys(self) -> list[str]:
        """Every data key a candidate of this profile can have: answers + external check results."""
        keys = self.question_keys()
        for check in self.checks:
            if check.field not in keys:
                keys.append(check.field)
        return keys

    def sensitive_keys(self) -> set[str]:
        return {q.field for q in self.questions if q.handler.sensitive}

    def complete_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Every data key of the profile (stable ATS contract): missing values are null / []."""
        list_keys = {q.field for q in self.questions if q.type == "multi_choice"}
        list_keys |= {q.tags_field for q in self.questions if q.tags_field}
        out = {key: data.get(key, [] if key in list_keys else None) for key in self.data_keys()}
        out.update({k: v for k, v in data.items() if k not in out})
        return out

    @property
    def flow(self) -> list[str]:
        return [Stage.INTRO] + [q.id for q in self.questions] + [Stage.REVIEW]

    def question(self, question_id: Optional[str]) -> Optional[QuestionSpec]:
        for q in self.questions:
            if q.id == question_id:
                return q
        return None

    def question_for_field(self, key: str) -> Optional[QuestionSpec]:
        for q in self.questions:
            if key in q.keys():
                return q
        return None

    def field_of_type(self, type_: str) -> Optional[str]:
        q = next((q for q in self.questions if q.type == type_), None)
        return q.field if q else None

    def check(self, check_id: Optional[str]) -> Optional[CheckSpec]:
        return next((c for c in self.checks if c.id == check_id), None)

    def checks_for_trigger(self, trigger: str) -> list[CheckSpec]:
        return [c for c in self.checks if c.trigger == trigger]

    def role_name(self, lang: str) -> str:
        return localize(self.role, lang, self.id)

    # ---------- knowledge ----------

    @cached_property
    def faq_index(self) -> Optional[FaqIndex]:
        if not self.faq:
            return None
        return FaqIndex([item.model_dump() for item in self.faq])

    @cached_property
    def areas(self) -> Optional[ServiceAreas]:
        if not self.service_areas:
            return None
        return ServiceAreas([area.model_dump() for area in self.service_areas])

    def context(self, today: date) -> FieldContext:
        return FieldContext(today=today, areas=self.areas)

    # ---------- decisions ----------

    def missing_required(self, data: dict[str, Any]) -> list[str]:
        return [q.field for q in self.questions if q.required and data.get(q.field) in (None, [], "")]

    def knockout(self, data: dict[str, Any], today: Optional[date] = None) -> Optional[Rule]:
        """First field-based disqualify rule matched by the data, if any."""
        for rule in self.rules:
            if rule.action == "disqualify" and rule.field and rule.matches(data, today):
                return rule
        return None

    def is_qualified(self, data: dict[str, Any], today: Optional[date] = None) -> bool:
        return self.knockout(data, today) is None and not self.missing_required(data)

    def pending_blocking_checks(self, data: dict[str, Any]) -> list[CheckSpec]:
        return [c for c in self.checks if c.blocking and data.get(c.field) == "pending"]

    def review_flags(self) -> set[str]:
        """Flags that send a qualified candidate to human review instead of the shortlist."""
        out: set[str] = set()
        for rule in self.rules:
            if rule.action == "flag" and rule.review:
                out.update(rule.flags or [rule.id])
        return out

    def rule(self, rule_id: Optional[str]) -> Optional[Rule]:
        return next((r for r in self.rules if r.id == rule_id), None)

    def score(self, data: dict[str, Any], today: Optional[date] = None) -> tuple[Optional[float], list[dict[str, Any]]]:
        """Ranking score 0-100 plus a per-rule breakdown for recruiters."""
        if not self.scoring:
            return None, []
        ceilings: dict[str, float] = {}
        for rule in self.scoring:
            ceilings[rule.field] = max(ceilings.get(rule.field, 0.0), rule.ceiling())
        total_max = sum(ceilings.values()) or 1.0
        raw = 0.0
        breakdown: list[dict[str, Any]] = []
        for rule in self.scoring:
            pts = rule.award(data, today)
            raw += pts
            breakdown.append({"field": rule.field, "label": rule.label, "points": round(pts, 1),
                              "max": round(rule.ceiling(), 1)})
        return round(min(100.0, 100.0 * raw / total_max), 1), breakdown

    # ---------- serialisation ----------

    def to_data(self) -> dict[str, Any]:
        """Normalised JSON stored as a published revision."""
        return self.model_dump(mode="json", exclude={"revision"})

    def public(self) -> dict[str, Any]:
        """Profile description for the candidate chat and the backoffice."""
        return {
            "id": self.id,
            "version": self.version,
            "revision": self.revision,
            "active": self.active,
            "company": self.company,
            "role": self.role,
            "summary": self.summary,
            "sector": self.sector,
            "agent_name": self.persona.agent_name,
            "languages": self.languages,
            "default_language": self.default_language,
            "questions": [
                {
                    "id": q.id, "field": q.field, "type": q.type, "required": q.required, "label": q.label,
                    "prompt": {lang: variants[0] for lang, variants in q.prompts.items() if variants},
                    "options": [o.model_dump() for o in q.options], "keys": q.keys(),
                    "min": q.min, "max": q.max, "max_days_ahead": q.max_days_ahead,
                    "tags_field": q.tags_field,
                }
                for q in self.questions
            ],
            "rules": [r.model_dump(exclude_none=True) for r in self.rules],
            "checks": [{**c.model_dump(exclude_none=True), "field": c.field} for c in self.checks],
            "scoring": [s.model_dump(exclude_none=True) for s in self.scoring],
            "has_faq": bool(self.faq),
            # short label per FAQ entry (its first question), for analytics and filters
            "faq_topics": {f.id: (f.q.split("?")[0] + "?" if "?" in f.q else f.q)[:80] for f in self.faq},
            "service_areas": self.areas.cities() if self.areas else [],
        }


# ---------- validation that reports every problem (for the editor) ----------

def _problem(path: list, message: str) -> dict[str, Any]:
    return {"path": path, "message": message}


def cross_problems(p: JobProfile) -> list[dict[str, Any]]:
    """Consistency checks across sections, each pointing at the offending item."""
    problems: list[dict[str, Any]] = []
    if not _SLUG.match(p.id):
        problems.append(_problem(["id"], f"id de proceso «{p.id}» no válido: minúsculas, números y _"))
    if not p.questions:
        problems.append(_problem(["questions"], "el proceso necesita al menos una pregunta"))
    seen_ids: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    for i, q in enumerate(p.questions):
        if q.id in seen_ids:
            problems.append(_problem(["questions", i, "id"], f"id de pregunta repetido «{q.id}»"))
        seen_ids[q.id] = i
        for key in q.keys():
            if key in seen_keys:
                problems.append(_problem(["questions", i, "field"], f"el dato «{key}» ya lo guarda otra pregunta"))
            seen_keys[key] = i
    if sum(1 for q in p.questions if q.type == "location") > 1:
        problems.append(_problem(["questions"], "solo se admite una pregunta de ubicación"))
    if p.default_language not in p.intro or not any(p.intro.get(p.default_language) or []):
        problems.append(_problem(["intro"], "falta el saludo inicial en el idioma por defecto"))

    question_ids = set(seen_ids)
    check_fields: set[str] = set()
    seen_checks: set[str] = set()
    for i, c in enumerate(p.checks):
        if c.id in seen_checks:
            problems.append(_problem(["checks", i, "id"], f"id de verificación repetido «{c.id}»"))
        seen_checks.add(c.id)
        if c.field in seen_keys or c.field in check_fields:
            problems.append(_problem(["checks", i, "result_field"], f"el dato «{c.field}» ya existe"))
        check_fields.add(c.field)
        if c.trigger != ON_COMPLETE and c.trigger not in question_ids:
            problems.append(_problem(["checks", i, "trigger"], f"la verificación «{c.id}» se dispara con una pregunta que no existe"))
        for key in c.inputs:
            q = p.question_for_field(key)
            if key not in seen_keys:
                problems.append(_problem(["checks", i, "inputs"], f"la verificación «{c.id}» envía un dato desconocido «{key}»"))
            elif q is not None and q.handler.sensitive and q.id != c.trigger:
                problems.append(_problem(["checks", i, "inputs"],
                                         f"«{key}» es un dato sensible: solo puede enviarse en una verificación disparada por su pregunta"))
        if c.provider == "webhook" and c.url and not c.url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            problems.append(_problem(["checks", i, "url"], "la URL del servicio debe ser https://"))
        if c.when is not None and c.when.field not in seen_keys:
            problems.append(_problem(["checks", i, "when"], f"condición sobre un dato desconocido «{c.when.field}»"))
        if c.when is not None and c.when.op not in _OPS:
            problems.append(_problem(["checks", i, "when"], f"operador desconocido «{c.when.op}»"))

    known = set(seen_keys) | check_fields
    seen_rules: set[str] = set()
    for i, r in enumerate(p.rules):
        if r.id in seen_rules:
            problems.append(_problem(["rules", i, "id"], f"id de regla repetido «{r.id}»"))
        seen_rules.add(r.id)
        if r.field and r.field not in known:
            problems.append(_problem(["rules", i, "field"], f"la regla «{r.id}» usa un dato desconocido «{r.field}»"))
        if r.question and r.question not in question_ids:
            problems.append(_problem(["rules", i, "question"], f"la regla «{r.id}» usa una pregunta desconocida «{r.question}»"))
        if r.action == "disqualify" and not any((r.message or {}).values()):
            problems.append(_problem(["rules", i, "message"], f"la regla eliminatoria «{r.id}» necesita un mensaje para el candidato"))
    for i, s in enumerate(p.scoring):
        if s.field not in known:
            problems.append(_problem(["scoring", i, "field"], f"el criterio de puntuación usa un dato desconocido «{s.field}»"))
    faq_ids: set[str] = set()
    for i, item in enumerate(p.faq):
        if item.id in faq_ids:
            problems.append(_problem(["faq", i, "id"], f"id de FAQ repetido «{item.id}»"))
        faq_ids.add(item.id)
    return problems


_PYDANTIC_MESSAGES = {
    "missing": "campo obligatorio",
    "extra_forbidden": "campo desconocido",
    "literal_error": "valor no permitido",
    "string_type": "debe ser texto",
    "bool_type": "debe ser sí/no",
    "bool_parsing": "debe ser sí/no",
    "int_type": "debe ser un número entero",
    "int_parsing": "debe ser un número entero",
    "float_type": "debe ser un número",
    "float_parsing": "debe ser un número",
    "list_type": "debe ser una lista",
    "dict_type": "formato no válido",
    "model_type": "formato no válido",
    "greater_than_equal": "valor demasiado pequeño",
    "less_than_equal": "valor demasiado grande",
}


def validate_profile_data(raw: Any) -> tuple[Optional[JobProfile], list[dict[str, Any]]]:
    """Validate a profile dict, returning (profile or None, every problem found).

    Used by the backoffice editor: drafts may be temporarily inconsistent, so
    problems are reported with the path of the offending cell instead of
    failing on the first one.
    """
    if not isinstance(raw, dict):
        return None, [_problem([], "el perfil debe ser un objeto JSON")]
    try:
        profile = JobProfile.model_validate(raw, context={"collect_problems": True})
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = [p for p in err["loc"] if p not in ("function-after",) and not str(p).startswith("function")]
            message = err.get("msg", "")
            if err["type"] == "value_error":
                message = str(message).removeprefix("Value error, ")
            else:
                message = _PYDANTIC_MESSAGES.get(err["type"], message)
            problems.append(_problem(loc, message))
        return None, problems
    problems = cross_problems(profile)
    return (profile if not problems else None), problems


# ---------- loading ----------

def load_seed(path: Path) -> dict[str, Any]:
    """Read a seed ``profile.json``, embedding its FAQ / coverage files as inline data."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key, target, inner in (("faq_file", "faq", "items"), ("service_areas_file", "service_areas", "locations")):
        name = raw.pop(key, None)
        if name:
            file = path.parent / name
            if not file.exists():
                raise ValueError(f"profile {raw.get('id')!r}: missing file {name}")
            raw[target] = json.loads(file.read_text(encoding="utf-8"))[inner]
    return raw


def load_profile(path: Path) -> JobProfile:
    return JobProfile.model_validate(load_seed(path))


def data_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class ProfileSource(Protocol):
    """Persistence used by the registry once attached (see ``storage/profile_store.py``)."""

    def sync_seeds(self, seeds: dict[str, dict[str, Any]]) -> None: ...
    def latest_all(self) -> dict[str, tuple[int, dict[str, Any]]]: ...
    def revision(self, profile_id: str, revision: int) -> Optional[dict[str, Any]]: ...


class ProfileRegistry:
    """Latest published revision of every profile, plus older revisions on demand.

    Without a store (tests, scripts) the seed folders are the profiles, as
    revision 1. With a store attached (the app), seeds are imported into the
    database and the database is the source of truth: drafts edited in the
    backoffice become new revisions when published.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or settings.profiles_dir)
        self.store: Optional[ProfileSource] = None
        self._latest: dict[str, JobProfile] = {}
        self._revisions: dict[tuple[str, int], JobProfile] = {}
        self.reload()

    def seeds(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for path in sorted(self.root.glob("*/profile.json")):
            raw = load_seed(path)
            pid = raw.get("id")
            if pid in out:
                raise ValueError(f"duplicate profile id {pid!r} in {path}")
            JobProfile.model_validate(raw)  # a broken seed fails fast, before anything is stored
            out[pid] = raw
        return out

    def attach_store(self, store: ProfileSource) -> None:
        self.store = store
        self.reload()
        self._stamp = self._read_stamp()
        self._checked_at = time.monotonic()

    def _read_stamp(self) -> Optional[str]:
        stamp = getattr(self.store, "stamp", None)
        return stamp() if callable(stamp) else None

    def refresh_if_changed(self, min_interval: float = 2.0) -> None:
        """Pick up revisions published by another process (throttled)."""
        if self.store is None or time.monotonic() - getattr(self, "_checked_at", 0) < min_interval:
            return
        self._checked_at = time.monotonic()
        stamp = self._read_stamp()
        if stamp != getattr(self, "_stamp", None):
            self.reload()
            self._stamp = stamp

    def latest(self, profile_id: str) -> Optional[JobProfile]:
        return self._latest.get(profile_id)

    def detach_store(self) -> None:
        self.store = None
        self.reload()

    def reload(self) -> None:
        latest: dict[str, JobProfile] = {}
        if self.store is None:
            for pid, raw in self.seeds().items():
                latest[pid] = JobProfile.model_validate(raw)
        else:
            self.store.sync_seeds(self.seeds())
            for pid, (rev, raw) in self.store.latest_all().items():
                try:
                    profile = JobProfile.model_validate(raw)
                except ValidationError:
                    log.exception("stored profile %s r%s is invalid; skipped", pid, rev)
                    continue
                profile.revision = rev
                latest[pid] = profile
        if not latest:
            raise ValueError(f"no screening profiles found under {self.root}")
        self._latest = latest
        self._revisions = {(p.id, p.revision): p for p in latest.values()}
        log.info("screening profiles: %s", ", ".join(f"{p.id} r{p.revision}" for p in latest.values()))

    def get(self, profile_id: Optional[str], revision: Optional[int] = None) -> JobProfile:
        if not profile_id:
            return self.default()
        latest = self._latest.get(profile_id)
        if revision is None or (latest is not None and latest.revision == revision):
            if latest is None:
                raise KeyError(profile_id)
            return latest
        cached = self._revisions.get((profile_id, revision))
        if cached is not None:
            return cached
        raw = self.store.revision(profile_id, revision) if self.store else None
        if raw is None:
            if latest is None:
                raise KeyError(profile_id)
            log.warning("revision %s of %s not found, using r%s", revision, profile_id, latest.revision)
            return latest
        profile = JobProfile.model_validate(raw)
        profile.revision = revision
        self._revisions[(profile_id, revision)] = profile
        return profile

    def default(self) -> JobProfile:
        return self._latest.get(settings.default_profile) or next(iter(self._latest.values()))

    def all(self, include_inactive: bool = True) -> list[JobProfile]:
        return [p for p in self._latest.values() if include_inactive or p.active]

    def __contains__(self, profile_id: object) -> bool:
        return profile_id in self._latest


_REGISTRY: Optional[ProfileRegistry] = None


def reset_registry() -> None:
    """Forget the process-wide registry (tests)."""
    global _REGISTRY
    _REGISTRY = None


def registry() -> ProfileRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ProfileRegistry()
    return _REGISTRY


def get_profile(profile_id: Optional[str] = None, revision: Optional[int] = None) -> JobProfile:
    return registry().get(profile_id, revision)
