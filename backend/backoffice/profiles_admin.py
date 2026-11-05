"""Recruiting-process administration: CRUD of screening profiles with drafts,
publication as immutable revisions, version history and a rule simulator.

Editing model:
- The backoffice edits a **draft** (a full profile JSON). Drafts may be
  temporarily inconsistent (e.g. a rule pointing at a question being renamed):
  every save returns the list of problems with the path of the offending cell.
- **Publishing** validates the draft and stores it as a new revision. New
  candidates use it immediately; candidates already in the funnel keep the
  revision they started with (their sessions are pinned to it).
- Older revisions can be viewed, exported and restored into the draft.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from ..llm.client import MockLLM
from ..screening.agent import ScreeningAgent
from ..screening.profile import (
    CHECK_RESULTS, ON_COMPLETE, JobProfile, ProfileRegistry, localize, validate_profile_data,
)
from ..storage.profile_store import DraftConflict, ProfileStore
from ..verifications.providers import MockIdRegistry
from .service import Invalid, NotFound

_ID = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

# Editor vocabulary (labels shown in the inline tables).
EDITOR_SCHEMA: dict[str, Any] = {
    "question_types": {
        "name": "Nombre", "yes_no": "Sí / No", "choice": "Opción única", "multi_choice": "Opción múltiple",
        "number": "Número", "experience": "Experiencia", "date": "Fecha", "location": "Ubicación",
        "text": "Texto libre", "id_document": "Documento de identidad",
    },
    "operators": {
        "eq": "=", "ne": "≠", "lt": "<", "lte": "≤", "gt": ">", "gte": "≥", "in": "es uno de", "not_in": "no es uno de",
        "contains": "incluye", "not_contains": "no incluye", "missing": "sin respuesta", "present": "respondida",
        "within_days": "en ≤ N días",
    },
    "providers": {"mock_id_registry": "Registro de identidad (simulado)", "webhook": "Servicio externo (webhook)",
                  "manual": "Manual (reclutador)"},
    "check_results": list(CHECK_RESULTS),
    "on_complete": ON_COMPLETE,
    "errors": {
        "name": ["empty", "placeholder", "format", "single_word"],
        "yes_no": ["unclear_yes_no"],
        "choice": ["unknown_option"],
        "multi_choice": ["unknown_option"],
        "number": ["unparsed_number", "range"],
        "experience": ["unparsed_years", "range"],
        "date": ["unparsed_date", "past_date", "too_far"],
        "location": ["ambiguous_city", "out_of_area"],
        "text": ["empty"],
        "id_document": ["invalid_id", "invalid_id_checksum"],
    },
    "documents": {"dni": "DNI", "nie": "NIE", "passport": "Pasaporte"},
    "messages": {
        "qualified": "Cierre: pre-calificado", "review": "Cierre: revisión / incompleto",
        "verifying": "Cierre: esperando verificaciones", "resume": "Al retomar la conversación",
        "handback": "Cuando el reclutador devuelve la conversación", "paused": "Mientras atiende un reclutador",
    },
}


def blank_profile(profile_id: str, company: str, role: str) -> dict[str, Any]:
    """Smallest useful process: name + start date, ready to be extended in the editor."""
    return {
        "id": profile_id,
        "version": "1",
        "active": True,
        "company": company,
        "role": {"es": role, "en": role},
        "summary": {"es": "", "en": ""},
        "sector": "",
        "persona": {"agent_name": "Alex",
                    "personality": "Friendly recruiting assistant. Short, plain messages; one question at a time.",
                    "context": ""},
        "languages": ["es", "en"],
        "default_language": "es",
        "intro": {"es": ["¡Hola! Soy {name}, de {company}. Te hago unas preguntas rápidas para el puesto de {role}. ¿Empezamos?"],
                  "en": ["Hi! I'm {name} from {company}. A few quick questions for the {role} job. Shall we start?"]},
        "messages": {
            "qualified": {"es": "¡Gracias! Cumples los requisitos: te contactamos en 24-48h.",
                          "en": "Thanks! You meet the requirements: we'll contact you within 24-48h."},
            "review": {"es": "Gracias. Un reclutador revisará tu caso y te contactará.",
                       "en": "Thanks. A recruiter will review your case and contact you."},
        },
        "questions": [
            {"id": "name", "field": "full_name", "type": "name", "label": {"es": "Nombre", "en": "Name"},
             "description": "Candidate full name if stated.",
             "prompts": {"es": ["¿Cuál es tu nombre completo?"], "en": ["What's your full name?"]}},
            {"id": "start_date", "field": "start_date", "type": "date",
             "label": {"es": "Fecha de inicio", "en": "Start date"}, "description": "Earliest start date.",
             "prompts": {"es": ["¿Desde qué fecha podrías empezar?"], "en": ["From which date could you start?"]}},
        ],
        "rules": [],
        "checks": [],
        "scoring": [],
        "faq": [],
        "service_areas": [],
        "max_stage_attempts": 3,
    }


class _SingleProfile:
    """Registry stand-in so the agent can run an unpublished draft (simulator)."""

    def __init__(self, profile: JobProfile):
        self.profile = profile

    def get(self, profile_id: Optional[str] = None, revision: Optional[int] = None) -> JobProfile:
        return self.profile

    def default(self) -> JobProfile:
        return self.profile

    def all(self, include_inactive: bool = True) -> list[JobProfile]:
        return [self.profile]


class ProfileAdmin:
    def __init__(self, registry: ProfileRegistry, store: ProfileStore):
        self.registry = registry
        self.store = store

    # ---------- reads ----------

    def list(self) -> list[dict[str, Any]]:
        out = []
        for row in self.store.list():
            latest = self.registry.latest(row["id"])
            draft = None
            if latest is None and row["has_draft"]:
                draft = (self.store.get(row["id"]) or {}).get("draft") or {}
            source = latest.to_data() if latest else (draft or {})
            out.append({
                **row,
                "company": source.get("company", ""),
                "role": source.get("role", {}),
                "summary": source.get("summary", {}),
                "agent_name": (source.get("persona") or {}).get("agent_name", ""),
                "active": bool(source.get("active", False)) and latest is not None,
                "published": latest is not None,
                "questions": len(source.get("questions") or []),
                "rules": len(source.get("rules") or []),
                "checks": len(source.get("checks") or []),
            })
        return out

    def get(self, profile_id: str) -> dict[str, Any]:
        row = self.store.get(profile_id)
        if row is None:
            raise NotFound(profile_id)
        working = row["draft"] or row["published"]
        _, problems = validate_profile_data(working)
        return {**row, "working": working, "problems": problems, "schema": EDITOR_SCHEMA}

    def version(self, profile_id: str, revision: int) -> dict[str, Any]:
        data = self.store.revision(profile_id, revision)
        if data is None:
            raise NotFound(f"{profile_id} r{revision}")
        return data

    # ---------- drafts ----------

    def save_draft(self, profile_id: str, data: dict[str, Any], by: str, base_revision: Optional[int]) -> dict[str, Any]:
        if self.store.get(profile_id) is None:
            raise NotFound(profile_id)
        if not isinstance(data, dict):
            raise Invalid("the profile must be a JSON object")
        data = dict(data)
        data["id"] = profile_id  # the id is the key of every stored candidate: it never changes
        try:
            revision = self.store.save_draft(profile_id, data, by, base_revision)
        except DraftConflict as exc:
            raise Conflict(str(exc)) from exc
        _, problems = validate_profile_data(data)
        return {"draft_revision": revision, "problems": problems}

    def discard_draft(self, profile_id: str) -> None:
        row = self.store.get(profile_id)
        if row is None:
            raise NotFound(profile_id)
        if not row["latest_revision"]:
            raise Invalid("este proceso aún no se ha publicado: bórralo en lugar de descartar el borrador")
        self.store.discard_draft(profile_id)

    def publish(self, profile_id: str, by: str, note: Optional[str] = None) -> dict[str, Any]:
        row = self.store.get(profile_id)
        if row is None:
            raise NotFound(profile_id)
        if not row["draft"]:
            raise Invalid("no hay cambios pendientes de publicar")
        profile, problems = validate_profile_data(row["draft"])
        if profile is None:
            raise ProblemsError(problems)
        revision = self.store.publish(profile_id, profile.to_data(), by, (note or "").strip()[:300] or None)
        self._reload()
        return {"id": profile_id, "revision": revision}

    def restore(self, profile_id: str, revision: int, by: str) -> dict[str, Any]:
        data = self.version(profile_id, revision)
        current = self.store.get(profile_id)
        return self.save_draft(profile_id, data, by, current["draft_revision"] if current and current["has_draft"] else None)

    def set_active(self, profile_id: str, active: bool, by: str) -> dict[str, Any]:
        """Quick (de)activation: publishes a revision that only changes ``active``."""
        row = self.store.get(profile_id)
        if row is None or not row["published"]:
            raise Invalid("solo se pueden activar procesos publicados")
        data = copy.deepcopy(row["published"])
        data["active"] = active
        profile, problems = validate_profile_data(data)
        if profile is None:
            raise ProblemsError(problems)
        revision = self.store.publish(profile_id, profile.to_data(), by,
                                      "Proceso activado" if active else "Proceso desactivado")
        if row["draft"]:  # keep an open draft consistent with the new state
            draft = dict(row["draft"])
            draft["active"] = active
            self.store.save_draft(profile_id, draft, by, None)
        self._reload()
        return {"id": profile_id, "revision": revision, "active": active}

    # ---------- create / delete ----------

    def create(self, profile_id: str, by: str, company: str, role: str,
               from_profile: Optional[str] = None) -> dict[str, Any]:
        if not _ID.match(profile_id or ""):
            raise Invalid("id no válido: 3-40 caracteres, minúsculas, números y _ (empieza por letra)")
        if from_profile:
            source = self.store.get(from_profile)
            if source is None:
                raise NotFound(from_profile)
            data = copy.deepcopy(source["draft"] or source["published"])
            data["id"] = profile_id
            if company:
                data["company"] = company
            if role:
                data["role"] = {"es": role, "en": role}
        else:
            if not company or not role:
                raise Invalid("indica la empresa y el puesto")
            data = blank_profile(profile_id, company, role)
        try:
            self.store.create(profile_id, data, by)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        return self.get(profile_id)

    def delete(self, profile_id: str) -> None:
        if self.store.get(profile_id) is None:
            raise NotFound(profile_id)
        try:
            self.store.delete(profile_id)
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        self._reload()

    def _reload(self) -> None:
        self.registry.reload()
        self.registry._stamp = self.store.stamp()

    # ---------- simulator ----------

    def simulate(self, profile_id: str, messages: list[str], source: str = "draft",
                 language: Optional[str] = None) -> dict[str, Any]:
        """Run a scripted conversation through the draft (or published) rules.

        Deterministic engine (no LLM, nothing stored): the point is to check the
        business rules before publishing. External checks answer instantly with
        the mock registry's test numbers; manual / webhook checks stay pending.
        """
        row = self.store.get(profile_id)
        if row is None:
            raise NotFound(profile_id)
        data = row["draft"] if source == "draft" and row["draft"] else row["published"]
        if data is None:
            raise Invalid("nada que simular")
        profile, problems = validate_profile_data(data)
        if profile is None:
            raise ProblemsError(problems)
        profile.revision = 0
        agent = ScreeningAgent(MockLLM(), _SingleProfile(profile))  # type: ignore[arg-type]
        state = agent.new_session(source="simulator", profile_id=profile_id, language=language)
        transcript = [{"role": "assistant", "content": agent.opening(state)}]
        registry_mock = MockIdRegistry(delay_seconds=0)
        for text in messages[:40]:
            reply, decision = agent.process(state, text)
            transcript.append({"role": "user", "content": text})
            if reply:
                transcript.append({"role": "assistant", "content": reply, "stage": decision.stage_after,
                                   "error": decision.validation_error})
            for req in decision.check_requests:
                check = profile.check(req["check"])
                if check.provider != "mock_id_registry":
                    transcript.append({"role": "system", "content": f"Verificación «{check.id}» solicitada "
                                       f"({check.provider}): se resolvería de forma asíncrona."})
                    continue
                status, detail = registry_mock.poll({"due_at": None}, req["payload"], None)  # type: ignore[arg-type]
                transcript.append({"role": "system", "content": f"Verificación «{check.id}»: {status} ({detail})"})
                late, _ = agent.apply_check_result(state, check.id, status, detail)
                if late:
                    transcript.append({"role": "assistant", "content": late, "async": True})
        score, breakdown = profile.score(state.data)
        return {
            "source": "draft" if data is row["draft"] else "published",
            "transcript": transcript,
            "stage": state.stage,
            "status": state.status,
            "pending_question": agent.question_text(state) if state.status == "active" else "",
            "data": dict(state.data),
            "flags": list(state.flags),
            "disqualify_reason": state.disqualify_reason,
            "score": score,
            "breakdown": [{**b, "label": localize(b["label"], "es", b["field"])} for b in breakdown],
        }


class Conflict(RuntimeError):
    pass


class ProblemsError(ValueError):
    def __init__(self, problems: list[dict[str, Any]]):
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


__all__ = ["ProfileAdmin", "Conflict", "ProblemsError", "EDITOR_SCHEMA", "blank_profile"]
