from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from ..config import settings
from ..llm.client import BaseLLM, LLMUnavailable
from .guardrails import boundary_message, redact_pii, screen_input, wants_human
from .language import detect_language, resolve_language
from .prompts import (
    AGENT_NAME,
    CLOSED_MESSAGES,
    DISQUALIFY_MESSAGES,
    REASK_MESSAGES,
    RESUME_MESSAGES,
    STAGE_QUESTIONS,
    SUMMARY_NOTE_PROMPT,
)
from .rag import FaqIndex
from .schema import (
    Availability,
    ExtractionResult,
    FLOW,
    Intent,
    Lang,
    Schedule,
    ScreeningData,
    Sentiment,
    SessionSummary,
    Stage,
    TERMINAL_STAGES,
    TurnDecision,
)
from .service_areas import AREAS, ServiceAreas
from .validation import (
    map_availability,
    map_schedule,
    map_yes_no,
    parse_date_text,
    parse_experience_fallback,
    parse_years,
    validate_name,
    validate_start_date,
    validate_years,
)

MAX_STAGE_ATTEMPTS = 3
MAX_ABUSE = 3

STAGE_FIELD: dict[Stage, str] = {
    Stage.NAME: "full_name",
    Stage.LICENSE: "has_license",
    Stage.LOCATION: "city",
    Stage.AVAILABILITY: "availability",
    Stage.SCHEDULE: "preferred_schedule",
    Stage.EXPERIENCE: "experience_years",
    Stage.START_DATE: "start_date",
}
OPTIONAL_STAGES = {Stage.EXPERIENCE}

STAGE_EMPTY_ERROR: dict[Stage, str] = {
    Stage.NAME: "empty",
    Stage.LICENSE: "unknown_license",
    Stage.LOCATION: "empty",
    Stage.AVAILABILITY: "unknown_availability",
    Stage.SCHEDULE: "unknown_schedule",
    Stage.START_DATE: "unparsed_date",
}

_LEAK_MARKERS = (
    "FAQ:",
    "Pending question:",
    "Question to ask:",
    "The previous answer was not valid",
    "Answer the candidate's question",
    "The screening is finished with outcome",
    "Set a polite boundary",
    "Say:",
)


@dataclass
class SessionState:
    id: str
    stage: Stage = Stage.INTRO
    data: ScreeningData = field(default_factory=ScreeningData)
    history: list[dict] = field(default_factory=list)
    language: Lang = Lang.ES
    attempts: dict[str, int] = field(default_factory=dict)
    abuse_count: int = 0
    reengage_attempts: int = 0
    status: str = "active"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_candidate_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    given_up: list[str] = field(default_factory=list)
    languages_seen: list[str] = field(default_factory=list)
    rag_hits: list[str] = field(default_factory=list)
    completed_stages: list[str] = field(default_factory=list)
    source: str = "web"
    summary: Optional[dict] = None
    dropped_at: Optional[str] = None

    def attempts_for(self, stage: Stage) -> int:
        return self.attempts.get(stage.value, 0)

    def bump(self, stage: Stage) -> int:
        self.attempts[stage.value] = self.attempts_for(stage) + 1
        return self.attempts[stage.value]


class ScreeningAgent:
    """Deterministic stage machine wrapped around an LLM for language understanding.

    The LLM never decides which question comes next or whether a candidate is
    qualified: it extracts and phrases. Validation, branching and disqualification
    are code, which is what makes the pipeline auditable for a recruiter team.
    """

    def __init__(self, llm: BaseLLM, areas: ServiceAreas = AREAS, faq: Optional[FaqIndex] = None):
        self.llm = llm
        self.areas = areas
        self.faq = faq or FaqIndex()

    # ---------- entry points ----------

    def new_session(self, source: str = "web", language: Optional[str] = None) -> SessionState:
        state = SessionState(id=str(uuid.uuid4()), source=source)
        if language in ("es", "en"):
            state.language = Lang(language)
        return state

    def opening(self, state: SessionState) -> str:
        text = self._question(Stage.INTRO, state, variant=0)
        state.history.append({"role": "assistant", "content": text})
        state.completed_stages.append(Stage.INTRO.value)
        return text

    def resume(self, state: SessionState) -> str:
        """Reopen an abandoned (silence-timeout) session at the stage the
        candidate dropped. Qualified/disqualified decisions stay final."""
        if state.stage in (Stage.QUALIFIED, Stage.DISQUALIFIED):
            raise ValueError("final decisions cannot be resumed")
        dropped = state.dropped_at or (state.summary or {}).get("dropped_at_stage")
        stage = Stage(dropped) if dropped else Stage.INTRO
        if stage in TERMINAL_STAGES or stage is Stage.REVIEW:
            stage = self._next_required_stage(state)
        state.stage = stage
        state.status = "active"
        state.summary = None
        state.dropped_at = None
        state.reengage_attempts = 0
        state.last_candidate_ts = datetime.now(timezone.utc)
        state.attempts.pop(stage.value, None)
        if stage.value in state.given_up:
            state.given_up.remove(stage.value)
        reply = f"{RESUME_MESSAGES[state.language.value]} {self._question(stage, state)}".strip()
        state.history.append({"role": "assistant", "content": reply})
        return reply

    def process(self, state: SessionState, user_text: str, today: Optional[date] = None) -> tuple[str, TurnDecision]:
        today = today or date.today()
        decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)

        if state.stage in TERMINAL_STAGES:
            reply = CLOSED_MESSAGES["review"][state.language.value]
            decision.events.append({"type": "ignored_after_close", "payload": {"outcome": state.stage.value}})
            return reply, decision

        screened = screen_input(user_text)
        clean = screened.sanitized
        if settings.pii_redaction:
            clean = redact_pii(clean)

        detected = detect_language(clean)
        reply_lang = resolve_language(detected, state.language)
        if detected is Lang.MIXED:
            decision.events.append({"type": "code_switch", "payload": {"detected": "mixed", "reply_in": reply_lang.value}})
        if reply_lang.value not in state.languages_seen:
            state.languages_seen.append(reply_lang.value)
        if state.languages_seen and reply_lang != state.language:
            decision.events.append({"type": "language_switch", "payload": {"to": reply_lang.value}})
        state.language = reply_lang
        decision.language = reply_lang

        if screened.reason == "input_too_long":
            decision.guardrail = "input_too_long"
            decision.events.append({"type": "guardrail", "payload": {"reason": "input_too_long"}})

        state.history.append({"role": "user", "content": clean})
        state.last_candidate_ts = datetime.now(timezone.utc)

        if screened.reason == "self_harm":
            state.flags.append("self_harm")
            state.notes.append("Candidate expressed self-harm risk; conversation closed with helpline numbers.")
            decision.events.append({"type": "guardrail", "payload": {"reason": "self_harm"}})
            return self._close(state, decision, Stage.ABANDONED, None, clean, override=boundary_message("self_harm", state.language.value)), decision

        if screened.reason == "no_meaningful_content":
            decision.guardrail = "no_meaningful_content"
            decision.events.append({"type": "guardrail", "payload": {"reason": "no_meaningful_content"}})
            return self._guardrail_reply(state, decision, "no_meaningful_content", clean), decision

        if screened.reason == "prompt_injection":
            decision.guardrail = "prompt_injection"
            decision.events.append({"type": "guardrail", "payload": {"reason": "prompt_injection"}})
            return self._guardrail_reply(state, decision, "prompt_injection", clean), decision

        try:
            extraction = self.llm.extract(state.history, state.stage, today)
        except LLMUnavailable:
            extraction = ExtractionResult(intent=Intent.UNKNOWN, language=reply_lang)
            state.flags.append("llm_unavailable")
            decision.events.append({"type": "llm_error", "payload": {"stage": state.stage.value}})

        decision.sentiment = extraction.sentiment

        if extraction.intent is Intent.INJECTION:
            decision.guardrail = "prompt_injection"
            decision.events.append({"type": "guardrail", "payload": {"reason": "prompt_injection"}})
            return self._guardrail_reply(state, decision, "prompt_injection", clean), decision
        if extraction.intent is Intent.ABUSE or screened.reason == "abusive_language":
            state.abuse_count += 1
            decision.guardrail = "abusive_language"
            decision.events.append({"type": "guardrail", "payload": {"reason": "abusive_language", "count": state.abuse_count}})
            if state.abuse_count >= MAX_ABUSE:
                return self._close(state, decision, Stage.ABANDONED, "abusive_language", clean), decision
            return self._guardrail_reply(state, decision, "abusive_language", clean), decision
        if extraction.intent is Intent.END:
            stage = Stage.QUALIFIED if state.data.is_qualified() else Stage.ABANDONED
            return self._close(state, decision, stage, None, clean), decision

        if wants_human(clean):
            state.flags.append("requested_human_callback")
            state.notes.append("Candidate asked for a human recruiter.")

        if extraction.intent is Intent.QUESTION:
            return self._answer_question(state, decision, extraction, clean), decision
        if extraction.intent is Intent.OFF_TOPIC:
            decision.guardrail = "off_topic"
            decision.events.append({"type": "guardrail", "payload": {"reason": "off_topic"}})
            return self._guardrail_reply(state, decision, "off_topic", clean), decision

        self._apply_extraction(state, extraction, decision, today, clean)
        closing = self._advance(state, decision)
        if closing is not None:
            return closing, decision

        plan = self._plan_for(state, decision)
        reply = self._phrase(state, decision, plan, clean)
        self._commit(state, reply, decision)
        return reply, decision

    # ---------- extraction -> data ----------

    def _apply_extraction(self, state: SessionState, ex: ExtractionResult, decision: TurnDecision, today: date, clean: str) -> None:
        data = state.data

        if ex.full_name:
            result = validate_name(ex.full_name)
            if result.ok:
                data.full_name = str(result.value)
            else:
                decision.validation_error = str(result.error)
                decision.events.append({"type": "validation_fail", "payload": {"field": "full_name", "error": result.error}})

        if ex.has_license is not None:
            data.has_license = bool(ex.has_license)
        elif state.stage is Stage.LICENSE:
            guess = map_yes_no(clean)
            if guess is not None:
                data.has_license = guess
                decision.events.append({"type": "field_fallback", "payload": {"field": "has_license", "source": "raw_text"}})

        if ex.city:
            self._apply_location(state, ex, decision)

        if ex.availability:
            values: list[Availability] = []
            for raw in ex.availability:
                try:
                    values.append(Availability(str(raw).strip().lower()))
                except ValueError:
                    continue
            if values:
                data.availability = values
            else:
                decision.validation_error = "unknown_availability"
        if not data.availability and state.stage is Stage.AVAILABILITY:
            mapped = map_availability(clean)
            if mapped:
                data.availability = mapped
                decision.validation_error = None
                decision.events.append({"type": "field_fallback", "payload": {"field": "availability", "source": "raw_text"}})

        if ex.preferred_schedule:
            try:
                data.preferred_schedule = Schedule(str(ex.preferred_schedule).strip().lower())
            except ValueError:
                sched = map_schedule(str(ex.preferred_schedule))
                if sched:
                    data.preferred_schedule = sched
                else:
                    decision.validation_error = "unknown_schedule"
        if data.preferred_schedule is None and state.stage is Stage.SCHEDULE:
            sched = map_schedule(clean)
            if sched:
                data.preferred_schedule = sched
                decision.validation_error = None
                decision.events.append({"type": "field_fallback", "payload": {"field": "preferred_schedule", "source": "raw_text"}})

        if ex.experience_years is not None:
            years = ex.experience_years if isinstance(ex.experience_years, int) else parse_years(str(ex.experience_years))
            result = validate_years(years)
            if result.ok and years is not None:
                data.experience_years = years
            else:
                decision.validation_error = str(result.error)
        if ex.experience_platforms:
            data.experience_platforms = [str(p).strip() for p in ex.experience_platforms if str(p).strip()][:8]
        if data.experience_years is None and state.stage is Stage.EXPERIENCE:
            years, platforms = parse_experience_fallback(clean)
            if years is not None and validate_years(years).ok:
                data.experience_years = years
                decision.validation_error = None
                decision.events.append({"type": "field_fallback", "payload": {"field": "experience_years", "source": "raw_text"}})
            if platforms and not data.experience_platforms:
                data.experience_platforms = platforms[:8]

        if ex.start_date:
            parsed = parse_date_text(str(ex.start_date), today)
            result = validate_start_date(parsed, today)
            if result.ok:
                data.start_date = result.value
            else:
                decision.validation_error = str(result.error)

        if ex.notes:
            state.notes.append(str(ex.notes)[:200])

    def _apply_location(self, state: SessionState, ex: ExtractionResult, decision: TurnDecision) -> None:
        raw_city = str(ex.city)
        match = self.areas.match_city(raw_city)
        if ex.zone:
            zone_match = self.areas.match_city(str(ex.zone))
        else:
            zone_match = None

        if match.matched:
            state.data.city = match.city
            state.data.country = match.country
            state.data.in_service_area = True
            state.data.zone = (ex.zone or match.zone or None)
            decision.events.append({"type": "location_match", "payload": {"city": match.city, "reason": match.reason, "confidence": round(match.confidence, 2)}})
            return

        if zone_match and zone_match.matched:
            state.data.city = zone_match.city
            state.data.in_service_area = True
            state.data.zone = str(ex.zone)
            decision.events.append({"type": "location_match", "payload": {"city": zone_match.city, "reason": "zone_alias"}})
            return

        if match.reason == "ambiguous":
            decision.validation_error = "ambiguous_city"
            decision.events.append({"type": "validation_fail", "payload": {"field": "city", "error": "ambiguous", "options": match.candidates}})
            state.flags.append(f"ambiguous_location:{','.join(match.candidates)}")
            return

        decision.validation_error = "out_of_area"
        decision.events.append({"type": "validation_fail", "payload": {"field": "city", "error": "out_of_area", "best": match.city, "confidence": round(match.confidence, 2)}})

    def _advance(self, state: SessionState, decision: TurnDecision) -> Optional[str]:
        """Move the state machine forward. Returns a closing reply when the
        screening ends on this turn, otherwise None."""
        current = state.stage
        error = decision.validation_error

        if state.data.has_license is False:
            return self._close(state, decision, Stage.DISQUALIFIED, "no_license", None)

        if error == "out_of_area" and state.attempts_for(Stage.LOCATION) >= 1:
            state.flags.append("waitlist_out_of_area")
            return self._close(state, decision, Stage.DISQUALIFIED, "out_of_area", None)

        field_name = STAGE_FIELD.get(current)
        still_missing = field_name is not None and getattr(state.data, field_name, None) in (None, [], "")
        if error is None and still_missing:
            error = STAGE_EMPTY_ERROR.get(current, "empty")
            decision.validation_error = error

        if error and current not in OPTIONAL_STAGES:
            attempts = state.bump(current)
            if attempts >= MAX_STAGE_ATTEMPTS:
                state.given_up.append(current.value)
                state.flags.append(f"unresolved:{current.value}")
                state.notes.append(f"Could not validate {field_name or current.value} after {attempts} attempts; moved on with a flag.")
                decision.events.append({"type": "give_up_field", "payload": {"stage": current.value, "attempts": attempts}})
            else:
                decision.stage_after = current
                return None

        if current in OPTIONAL_STAGES and error:
            state.bump(current)
            state.given_up.append(current.value)
            state.flags.append(f"unresolved:{current.value}")
            decision.validation_error = None
            decision.events.append({"type": "give_up_field", "payload": {"stage": current.value, "attempts": state.attempts_for(current)}})

        target = self._next_required_stage(state)

        if target is Stage.REVIEW:
            if current.value not in state.completed_stages:
                state.completed_stages.append(current.value)
            if state.data.is_qualified():
                return self._close(state, decision, Stage.QUALIFIED, None, None)
            state.flags.append("needs_review")
            state.notes.append(
                "All questions were asked but some required fields could not be validated; recruiter review needed."
            )
            decision.events.append({"type": "needs_review", "payload": {"missing": state.data.missing_required()}})
            return self._close(state, decision, Stage.ABANDONED, None, None)

        if target != current:
            if current.value not in state.completed_stages:
                state.completed_stages.append(current.value)
            decision.advanced = True
            decision.events.append({"type": "stage_advance", "payload": {"from": current.value, "to": target.value}})
        state.stage = target
        return None

    def _next_required_stage(self, state: SessionState) -> Stage:
        for stage in FLOW:
            if stage in (Stage.INTRO, Stage.REVIEW):
                continue
            if stage.value in state.given_up:
                continue
            field_name = STAGE_FIELD[stage]
            value = getattr(state.data, field_name, None)
            if value in (None, [], ""):
                return stage
        return Stage.REVIEW

    # ---------- replies ----------

    def _answer_question(self, state: SessionState, decision: TurnDecision, ex: ExtractionResult, clean: str) -> str:
        question = ex.candidate_question or clean
        pending = self._question(state.stage, state)
        docs = self.faq.search(question, lang=state.language.value, k=2)
        if docs:
            ids = [d.id for d in docs]
            decision.rag_used = ids
            state.rag_hits.extend(ids)
            decision.events.append({"type": "rag_hit", "payload": {"ids": ids}})
            faq_block = "\n".join(f"- [{d.id}] {d.answer}" for d in docs)
            plan = f"Answer the candidate's question using only this FAQ context, then ask them the pending question again.\nFAQ:\n{faq_block}\nPending question: {pending}"
            fallback = f"{docs[0].answer}\n\n{pending}"
        else:
            faq_block = ""
            decision.events.append({"type": "rag_miss", "payload": {"q": question[:120]}})
            plan = (
                "The candidate asked a question that is not in the FAQ. Say you are not sure, offer a recruiter callback, "
                f"then ask the pending question again: {pending}"
            )
            fallback = pending
        reply = self._phrase(state, decision, plan, clean, fallback=fallback, faq_block=faq_block)
        self._commit(state, reply, decision)
        return reply

    def _guardrail_reply(self, state: SessionState, decision: TurnDecision, reason: str, clean: str) -> str:
        fallback = boundary_message(reason, state.language.value).format(next=self._question(state.stage, state))
        plan = (
            f"Set a polite boundary: the candidate's message is {reason}. Do not engage with it. "
            f"Then ask the pending question: {self._question(state.stage, state)}"
        )
        reply = self._phrase(state, decision, plan, clean, fallback=fallback)
        self._commit(state, reply, decision)
        return reply

    def _plan_for(self, state: SessionState, decision: TurnDecision) -> str:
        stage = state.stage
        if stage in (Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED):
            return ""

        question = self._question(stage, state)
        if decision.validation_error:
            return f"The previous answer was not valid ({decision.validation_error}). Say: {self._template_reply(state, decision)}"

        tone = ""
        if decision.sentiment in (Sentiment.FRUSTRATED, Sentiment.CONFUSED):
            tone = "The candidate sounds frustrated or confused: be shorter and warmer, and offer a recruiter callback."
        return f"Ask the candidate the next screening question. {tone}\nQuestion to ask: {question}"

    def _template_reply(self, state: SessionState, decision: TurnDecision) -> str:
        if decision.validation_error:
            template = REASK_MESSAGES.get(decision.validation_error, REASK_MESSAGES["empty"])
            text = template.get(state.language.value, template["es"])
            return text.format(
                next=self._question(state.stage, state),
                options=", ".join(self._ambiguous_options(decision)) or "varias ciudades",
                city=state.data.city or "esa ciudad",
            )
        return self._question(state.stage, state)

    def _phrase(self, state: SessionState, decision: TurnDecision, plan: str, clean: str, fallback: Optional[str] = None, faq_block: str = "") -> str:
        if not plan:
            return fallback or self._template_reply(state, decision)
        try:
            reply = self.llm.generate(
                stage=state.stage,
                language=state.language.value,
                sentiment=decision.sentiment,
                plan=plan,
                faq_block=faq_block,
                candidate_message=clean,
                state=self._state_digest(state),
            )
        except LLMUnavailable:
            state.flags.append("llm_unavailable")
            return fallback or self._template_reply(state, decision)
        if any(marker in reply for marker in _LEAK_MARKERS):
            decision.events.append({"type": "phrasing_fallback", "payload": {"reason": "prompt_leak"}})
            return fallback or self._template_reply(state, decision)
        return reply

    def _commit(self, state: SessionState, reply: str, decision: TurnDecision) -> None:
        state.history.append({"role": "assistant", "content": reply})
        decision.stage_after = state.stage

    def _close(self, state: SessionState, decision: TurnDecision, stage: Stage, reason: Optional[str],
               clean: Optional[str], override: Optional[str] = None) -> str:
        if state.stage not in TERMINAL_STAGES:
            state.dropped_at = state.stage.value
        state.stage = stage
        state.status = stage.value
        decision.stage_after = stage
        if reason:
            decision.disqualify_reason = reason
            state.flags.append(reason)
        if stage is Stage.QUALIFIED and Stage.REVIEW.value not in state.completed_stages:
            state.completed_stages.append(Stage.REVIEW.value)

        if override:
            fallback = override
        elif stage is Stage.DISQUALIFIED and reason in DISQUALIFY_MESSAGES:
            fallback = DISQUALIFY_MESSAGES[reason][state.language.value]
        elif stage is Stage.QUALIFIED:
            fallback = CLOSED_MESSAGES["qualified"][state.language.value]
        elif stage is Stage.ABANDONED:
            fallback = CLOSED_MESSAGES["review"][state.language.value]
        else:
            fallback = CLOSED_MESSAGES["review"][state.language.value]

        plan = f"The screening is finished with outcome {stage.value}. Close the conversation warmly and state the next step. Say: {fallback}"
        reply = self._phrase(state, decision, plan, clean or "", fallback=fallback)
        state.history.append({"role": "assistant", "content": reply})
        decision.events.append({"type": "session_close", "payload": {"outcome": stage.value, "reason": reason}})
        state.summary = self.summarize(state).model_dump(mode="json")
        return reply

    # ---------- summary ----------

    def summarize(self, state: SessionState) -> SessionSummary:
        duration = int((datetime.now(timezone.utc) - state.started_at).total_seconds())
        candidate_turns = sum(1 for m in state.history if m.get("role") == "user")
        outcome = state.stage.value
        if outcome in (Stage.QUALIFIED.value, Stage.DISQUALIFIED.value) or "needs_review" in state.flags:
            dropped = None
        else:
            dropped = state.dropped_at or outcome
        data = state.data.model_dump(mode="json")
        summary = SessionSummary(
            qualified=state.data.is_qualified(),
            outcome=outcome,
            disqualify_reason=next((f for f in ("no_license", "out_of_area") if f in state.flags), None),
            completed_stages=list(state.completed_stages),
            dropped_at_stage=dropped,
            duration_seconds=duration,
            candidate_turns=candidate_turns,
            languages=list(state.languages_seen),
            data=data,
            recruiter_notes=list(state.notes),
            flags=list(state.flags),
        )
        if summary.qualified and self.llm.available():
            summary.recruiter_notes.append(self._recruiter_note(summary))
        return summary

    def _recruiter_note(self, summary: SessionSummary) -> str:
        language = summary.languages[0] if summary.languages else "es"
        compact = {k: v for k, v in summary.data.items() if v not in (None, [], "")}
        try:
            note = self.llm.generate(
                stage=Stage.REVIEW,
                language=language,
                sentiment=Sentiment.NEUTRAL,
                plan=SUMMARY_NOTE_PROMPT.format(language=language, data=compact),
                faq_block="",
                candidate_message="",
                state="",
            )
        except LLMUnavailable:
            return ""
        return note[:200]

    # ---------- helpers ----------

    def _question(self, stage: Stage, state: SessionState, variant: Optional[int] = None) -> str:
        options = STAGE_QUESTIONS.get(stage)
        if not options:
            return ""
        variants = options.get(state.language.value) or options["es"]
        idx = variant if variant is not None else state.attempts_for(stage) % len(variants)
        return variants[idx % len(variants)].format(name=AGENT_NAME)

    def _ambiguous_options(self, decision: TurnDecision) -> list[str]:
        for event in reversed(decision.events):
            if event.get("type") == "validation_fail" and event["payload"].get("error") == "ambiguous":
                return event["payload"].get("options", [])
        return []

    def _state_digest(self, state: SessionState) -> str:
        filled = {k: v for k, v in state.data.model_dump().items() if v not in (None, [], "")}
        return str(filled)[:600]
