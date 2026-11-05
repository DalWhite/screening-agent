"""Recruiter actions on candidates.

Every action is applied to the *live* session (the same in-memory state the
agent and the channels use, rehydrated from SQLite when needed) and leaves an
audit event, so the activity timeline shows who did what and when.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..pipeline import STAGE_COLORS
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.profile import localize
from ..screening.schema import TERMINAL_STAGES, TurnDecision
from ..session_store import SessionStore
from ..storage import db
from ..verifications.service import VerificationError
from .labels import CHECK_STATUS_LABELS, PROVIDER_LABELS, SOURCE_LABELS, STATUS_LABELS, all_flag_labels
from .report import build_report

if TYPE_CHECKING:  # pragma: no cover
    from ..verifications.service import VerificationService

log = logging.getLogger("sazon.backoffice")

MAX_MESSAGE_CHARS = 1000


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


Notifier = Callable[[SessionState, str], bool]


class BackofficeService:
    def __init__(self, agent: ScreeningAgent, store: SessionStore, notifier: Optional[Notifier] = None,
                 verifications: Optional["VerificationService"] = None):
        self.agent = agent
        self.store = store
        self.notifier = notifier
        self.verifications = verifications

    # ---------- reads ----------

    def meta(self) -> dict[str, Any]:
        return {
            "pipeline": db.list_pipeline_stages(),
            "stage_colors": list(STAGE_COLORS),
            "profiles": [p.public() for p in self.agent.profiles.all()],
            "statuses": STATUS_LABELS,
            "check_statuses": CHECK_STATUS_LABELS,
            "providers": PROVIDER_LABELS,
            "sources": SOURCE_LABELS,
            "flags": all_flag_labels(),
            "tags": db.all_tags(),
            "assignees": db.all_assignees(),
        }

    def search(self, filters: db.SearchFilters) -> dict[str, Any]:
        result = db.search_sessions(filters)
        result["items"] = self._cards(result["items"])
        return result

    def pipeline(self, filters: db.SearchFilters, per_column: int = 50) -> dict[str, Any]:
        columns = []
        for stage in db.list_pipeline_stages():
            f = db.SearchFilters(**{**filters.__dict__, "pipeline_stage": [stage["id"]], "page": 1,
                                    "page_size": per_column})
            result = db.search_sessions(f, with_facets=False)
            columns.append({**stage, "count": result["total"], "items": self._cards(result["items"])})
        return {"columns": columns}

    def detail(self, session_id: str) -> dict[str, Any]:
        session = db.get_session(session_id)
        if session is None:
            raise NotFound(session_id)
        try:
            # the revision that screened this candidate, not necessarily the latest one
            profile = self.agent.profiles.get(session["profile_id"], session.get("profile_revision"))
        except KeyError as exc:
            raise NotFound(f"profile {session['profile_id']}") from exc
        state = self.store.get(session_id)
        pending_question = self.agent.question_text(state) if state and state.status == "active" else ""
        session["notes_recruiter"] = db.list_notes(session_id)
        session["verifications"] = self.verifications.for_session(session_id) if self.verifications else []
        session["report"] = build_report(profile, session, verifications=session["verifications"])
        session["profile"] = {"id": profile.id, "revision": profile.revision, "company": profile.company,
                              "role": profile.role,
                              "agent_name": profile.persona.agent_name,
                              "questions": [{"id": q.id, "label": q.label, "type": q.type, "keys": q.keys(),
                                             "options": [o.model_dump() for o in q.options], "field": q.field,
                                             "tags_field": q.tags_field}
                                            for q in profile.questions]}
        session["pending_question"] = pending_question
        session["chat_link"] = f"/?session={session_id}"
        return session

    def _cards(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compact candidate rows for lists and boards."""
        last_roles = self._last_message_roles([r["id"] for r in rows])
        cards = []
        for r in rows:
            try:
                role = localize(self.agent.profiles.get(r["profile_id"]).role, "es")
            except KeyError:
                role = r["profile_id"]
            cards.append({
                "id": r["id"],
                "profile_id": r["profile_id"],
                "role": role,
                "name": r.get("display_name"),
                "city": r.get("display_city"),
                "contact": r.get("contact"),
                "status": r["status"],
                "stage": r["stage"],
                "pipeline_stage": r.get("pipeline_stage"),
                "pipeline_manual": r.get("pipeline_manual"),
                "score": r.get("score"),
                "qualified": r.get("qualified"),
                "disqualify_reason": r.get("disqualify_reason"),
                "flags": r.get("flags") or [],
                "tags": r.get("tags") or [],
                "assignee": r.get("assignee"),
                "source": r["source"],
                "language": r["language"],
                "languages": r.get("languages") or [],
                "started_at": r["started_at"],
                "last_ts": r["last_ts"],
                "updated_at": r.get("updated_at"),
                "interview_at": r.get("interview_at"),
                "candidate_turns": r.get("candidate_turns"),
                "duration_seconds": r.get("duration_seconds"),
                "reengage_attempts": r.get("reengage_attempts"),
                "bot_paused": r.get("bot_paused"),
                "awaiting_reply": bool(r.get("bot_paused")) and last_roles.get(r["id"]) == "user",
            })
        return cards

    @staticmethod
    def _last_message_roles(ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT session_id, role FROM messages WHERE id IN (SELECT MAX(id) FROM messages "
                f"WHERE session_id IN ({','.join('?' * len(ids))}) GROUP BY session_id)",
                ids,
            ).fetchall()
        return {r["session_id"]: r["role"] for r in rows}

    def export_csv(self, filters: db.SearchFilters) -> str:
        filters.page, filters.page_size = 1, 1000
        rows = db.search_sessions(filters, with_facets=False)["items"]
        profiles = {p.id: p for p in self.agent.profiles.all()}
        data_keys: list[str] = []
        for p in profiles.values():
            for key in p.data_keys():
                if key not in data_keys:
                    data_keys.append(key)
        out = io.StringIO()
        writer = csv.writer(out)
        base = ["id", "profile_id", "name", "city", "contact", "source", "language", "status", "pipeline_stage",
                "score", "qualified", "disqualify_reason", "flags", "tags", "assignee", "interview_at",
                "started_at", "last_ts"]
        writer.writerow(base + data_keys)
        for r in rows:
            data = r.get("data") or {}
            writer.writerow([_safe_cell(v) for v in [
                r["id"], r["profile_id"], r.get("display_name") or "", r.get("display_city") or "",
                r.get("contact") or "", r["source"], r["language"], r["status"], r.get("pipeline_stage"),
                "" if r.get("score") is None else r["score"], "yes" if r.get("qualified") else "no",
                r.get("disqualify_reason") or "", "|".join(r.get("flags") or []), "|".join(r.get("tags") or []),
                r.get("assignee") or "", r.get("interview_at") or "", r["started_at"], r["last_ts"],
            ] + [_csv_value(data.get(k)) for k in data_keys]])
        return out.getvalue()

    # ---------- pipeline ----------

    # ---------- configurable stages ----------

    def stages(self) -> list[dict[str, Any]]:
        return db.list_pipeline_stages()

    def create_stage(self, label: str, color: str, after: Optional[str], by: str) -> dict[str, Any]:
        try:
            return db.create_pipeline_stage(label, color or "indigo", after)
        except db.StageError as exc:
            raise Invalid(str(exc)) from exc

    def update_stage(self, stage_id: str, by: str, label: Optional[str] = None, color: Optional[str] = None) -> dict[str, Any]:
        try:
            return db.update_pipeline_stage(stage_id, label, color)
        except KeyError as exc:
            raise NotFound(stage_id) from exc
        except db.StageError as exc:
            raise Invalid(str(exc)) from exc

    def reorder_stages(self, ids: list[str], by: str) -> list[dict[str, Any]]:
        try:
            db.reorder_pipeline_stages(ids)
        except db.StageError as exc:
            raise Invalid(str(exc)) from exc
        return db.list_pipeline_stages()

    def delete_stage(self, stage_id: str, move_to: Optional[str], by: str) -> dict[str, Any]:
        try:
            moved = db.delete_pipeline_stage(stage_id, move_to)
        except KeyError as exc:
            raise NotFound(stage_id) from exc
        except db.StageError as exc:
            raise Invalid(str(exc)) from exc
        for session_id in moved:
            self._event(session_id, "pipeline_change", {"from": stage_id, "to": move_to, "by": by,
                                                        "reason": "stage_deleted"})
        return {"deleted": stage_id, "moved": len(moved)}

    def move(self, session_id: str, stage: str, by: str) -> dict[str, Any]:
        if stage not in db.pipeline_ids():
            raise Invalid(f"unknown pipeline stage {stage!r}")
        previous = db.set_pipeline_stage(session_id, stage)
        if previous is None:
            raise NotFound(session_id)
        if previous != stage:
            self._event(session_id, "pipeline_change", {"from": previous, "to": stage, "by": by})
        return {"id": session_id, "pipeline_stage": stage, "previous": previous}

    def update(self, session_id: str, by: str, **changes: Any) -> dict[str, Any]:
        if db.get_session(session_id) is None:
            raise NotFound(session_id)
        result: dict[str, Any] = {"id": session_id}
        if "pipeline_stage" in changes and changes["pipeline_stage"] is not None:
            result.update(self.move(session_id, changes.pop("pipeline_stage"), by))
        else:
            changes.pop("pipeline_stage", None)
        clean: dict[str, Any] = {}
        if "assignee" in changes:
            clean["assignee"] = (str(changes["assignee"] or "").strip()[:80]) or None
        if "tags" in changes:
            tags = changes["tags"] or []
            if not isinstance(tags, list):
                raise Invalid("tags must be a list")
            clean["tags"] = list(dict.fromkeys(str(t).strip()[:40] for t in tags if str(t).strip()))[:20]
        if "interview_at" in changes:
            value = changes["interview_at"]
            if value:
                try:
                    datetime.fromisoformat(str(value))
                except ValueError as exc:
                    raise Invalid("interview_at must be an ISO date-time") from exc
            clean["interview_at"] = value or None
        if clean:
            db.update_recruiter_fields(session_id, **clean)
            self._event(session_id, "recruiter_update", {**clean, "by": by})
            result.update(clean)
        return result

    def bulk(self, ids: list[str], action: str, value: Any, by: str) -> dict[str, Any]:
        done: list[str] = []
        for session_id in ids[:200]:
            session = db.get_session(session_id)
            if session is None:
                continue
            if action == "move":
                self.move(session_id, str(value), by)
            elif action == "tag":
                tags = list(session.get("tags") or [])
                if value and value not in tags:
                    self.update(session_id, by, tags=tags + [str(value)])
            elif action == "assign":
                self.update(session_id, by, assignee=value)
            else:
                raise Invalid(f"unknown bulk action {action!r}")
            done.append(session_id)
        return {"updated": done, "count": len(done)}

    # ---------- notes ----------

    def add_note(self, session_id: str, body: str, by: str) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise Invalid("empty note")
        if db.get_session(session_id) is None:
            raise NotFound(session_id)
        note = db.add_note(session_id, by, body[:2000])
        self._event(session_id, "note_added", {"note_id": note["id"], "by": by})
        return note

    def delete_note(self, session_id: str, note_id: str, by: str) -> None:
        if not db.delete_note(session_id, note_id):
            raise NotFound(note_id)
        self._event(session_id, "note_deleted", {"note_id": note_id, "by": by})

    # ---------- candidate data ----------

    def edit_fields(self, session_id: str, changes: dict[str, Any], by: str) -> dict[str, Any]:
        state = self._state(session_id)
        profile = self.agent.profile(state)
        ctx = profile.context(date.today())
        applied: dict[str, Any] = {}
        with self.store.lock(state.id):
            decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
            edited_questions: list[str] = []
            for key, value in (changes or {}).items():
                q = profile.question_for_field(key)
                if q is None:
                    raise Invalid(f"unknown field {key!r}")
                try:
                    updates = q.handler.coerce_manual(q, key, value, ctx)
                except ValueError as exc:
                    raise Invalid(f"{key}: {exc}") from exc
                if q.handler.sensitive and value not in (None, ""):
                    decision.secrets.update(q.handler.validate(q, value).secrets)
                for k, v in updates.items():
                    old = state.data.get(k)
                    if old != v:
                        state.data[k] = v
                        applied[k] = {"from": old, "to": v}
                        if q.id not in edited_questions:
                            edited_questions.append(q.id)
            if applied:
                # a corrected answer re-runs the external checks it triggers (e.g. a new ID number)
                self.agent._trigger_checks(state, profile, decision, date.today(), edited_questions)
                db.save_session(state)
                if self.verifications is not None:
                    self.verifications.request_from_decision(state, decision)
                events = [{"type": "field_edit", "payload": {"changes": applied, "by": by}}] + decision.events
                db.append_events(session_id, events, state.stage)
        return {"id": session_id, "changes": applied, "data": dict(state.data)}

    # ---------- external checks ----------

    def decide_verification(self, session_id: str, verification_id: str, status: str, note: Optional[str],
                            by: str) -> dict[str, Any]:
        """Recruiter answer for a manual check, or an override of a provider result."""
        if self.verifications is None:
            raise Invalid("verifications are not enabled")
        row = self.verifications.get(verification_id)
        if row is None or row["session_id"] != session_id:
            raise NotFound(verification_id)
        if status not in ("valid", "invalid"):
            raise Invalid("status must be valid or invalid")
        try:
            return self.verifications.complete(verification_id, status, note or f"Decidido por {by}", by=by)
        except VerificationError as exc:
            raise Invalid(str(exc)) from exc

    def retry_verification(self, session_id: str, verification_id: str, by: str) -> dict[str, Any]:
        if self.verifications is None:
            raise Invalid("verifications are not enabled")
        row = self.verifications.get(verification_id)
        if row is None or row["session_id"] != session_id:
            raise NotFound(verification_id)
        try:
            new_id = self.verifications.retry(verification_id)
        except VerificationError as exc:
            raise Invalid(str(exc)) from exc
        self._event(session_id, "check_retry", {"verification": verification_id, "by": by})
        return {"id": new_id}

    def create_candidate(self, profile_id: str, by: str, name: Optional[str] = None, contact: Optional[str] = None,
                         language: Optional[str] = None, note: Optional[str] = None) -> dict[str, Any]:
        try:
            state = self.agent.new_session(source="backoffice", language=language, contact=(contact or "").strip() or None,
                                           profile_id=profile_id)
        except KeyError as exc:
            raise Invalid(f"unknown profile {profile_id!r}") from exc
        profile = self.agent.profile(state)
        if name:
            name_field = profile.field_of_type("name")
            if name_field:
                state.data[name_field] = name.strip()[:100]
        opening = self.agent.opening(state)
        self.store.put(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", opening, state.stage)
        self._event(state.id, "created_by_recruiter", {"by": by, "profile_id": profile.id}, stage=state.stage)
        if note:
            self.add_note(state.id, note, by)
        return {"id": state.id, "chat_link": f"/?session={state.id}"}

    # ---------- manual interaction ----------

    def send_message(self, session_id: str, text: str, by: str, pause_bot: bool = True) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise Invalid("empty message")
        if len(text) > MAX_MESSAGE_CHARS:
            raise Invalid(f"message longer than {MAX_MESSAGE_CHARS} characters")
        state = self._state(session_id)
        paused_now = False
        if pause_bot and not state.bot_paused:
            state.bot_paused = True
            paused_now = True
        state.history.append({"role": "assistant", "content": text})
        delivered = self._push(state, text)
        db.save_session(state)
        db.append_message(state.id, "recruiter", text, state.stage, delivered=delivered, author=by)
        events = [{"type": "recruiter_message", "payload": {"by": by, "pushed": delivered, "chars": len(text)}}]
        if paused_now:
            events.insert(0, {"type": "takeover_start", "payload": {"by": by}})
        db.append_events(state.id, events, state.stage)
        return {"id": session_id, "delivered": delivered, "bot_paused": state.bot_paused}

    def takeover(self, session_id: str, paused: bool, by: str) -> dict[str, Any]:
        state = self._state(session_id)
        if paused == state.bot_paused:
            return {"id": session_id, "bot_paused": state.bot_paused, "message": None}
        message = None
        if paused:
            state.bot_paused = True
            db.save_session(state)
            db.append_events(state.id, [{"type": "takeover_start", "payload": {"by": by}}], state.stage)
        else:
            if state.stage in TERMINAL_STAGES:
                state.bot_paused = False
            else:
                message = self.agent.handback(state)
            delivered = self._push(state, message) if message else False
            db.save_session(state)
            if message:
                db.append_message(state.id, "assistant", message, state.stage, delivered=delivered)
            db.append_events(state.id, [{"type": "takeover_end", "payload": {"by": by}}], state.stage)
        return {"id": session_id, "bot_paused": state.bot_paused, "message": message}

    def reopen(self, session_id: str, by: str, question_id: Optional[str] = None) -> dict[str, Any]:
        state = self._state(session_id)
        previous = state.status
        try:
            message = self.agent.reopen(state, question_id)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        state.bot_paused = False
        delivered = self._push(state, message)
        db.save_session(state)
        db.append_message(state.id, "assistant", message, state.stage, delivered=delivered)
        db.append_events(state.id, [{"type": "recruiter_reopen",
                                     "payload": {"by": by, "from_status": previous, "question": state.stage}}],
                         state.stage)
        return {"id": session_id, "status": state.status, "stage": state.stage, "message": message}

    # ---------- helpers ----------

    def _state(self, session_id: str) -> SessionState:
        state = self.store.get(session_id)
        if state is None:
            raise NotFound(session_id)
        return state

    def _push(self, state: SessionState, text: str) -> bool:
        """Push channels (WhatsApp) deliver now; web chat picks it up via /pending."""
        if self.notifier is None:
            return False
        try:
            return bool(self.notifier(state, text))
        except Exception:  # a failed push must not lose the message: it stays queued
            log.exception("push to candidate failed for session %s", state.id)
            return False

    @staticmethod
    def _event(session_id: str, type_: str, payload: dict[str, Any], stage: Optional[str] = None) -> None:
        db.append_events(session_id, [{"type": type_, "payload": payload}], stage)


def _safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas in candidate-controlled text (CSV injection)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
