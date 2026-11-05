from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from ..config import settings
from ..llm.client import BaseLLM, LLMUnavailable
from .fields import FieldOutcome
from .guardrails import boundary_message, redact_pii, screen_input, wants_human
from .language import detect_language, resolve_language
from .profile import ON_COMPLETE, JobProfile, ProfileRegistry, QuestionSpec, Rule, localize, registry
from .prompts import DEFAULT_REASK, DEFAULT_REENGAGE, SUMMARY_NOTE_PROMPT
from .schema import (
    CandidateData,
    ExtractionResult,
    FINAL_STAGES,
    Intent,
    Lang,
    Sentiment,
    SessionSummary,
    Stage,
    TERMINAL_STAGES,
    TurnDecision,
)

MAX_ABUSE = 3

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


_REDACTED = re.compile(r"\[(?:id|email) redacted\]")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(text: str, **values: Any) -> str:
    return text.format_map(_SafeDict(values))


@dataclass
class SessionState:
    id: str
    profile_id: str = ""
    profile_revision: Optional[int] = None  # the published rules that screen this candidate
    stage: str = Stage.INTRO
    data: CandidateData = field(default_factory=CandidateData)
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
    contact: Optional[str] = None
    summary: Optional[dict] = None
    dropped_at: Optional[str] = None
    disqualify_reason: Optional[str] = None
    close_reason: Optional[str] = None
    bot_paused: bool = False  # human takeover from the backoffice: the agent listens but does not reply

    def attempts_for(self, stage: str) -> int:
        return self.attempts.get(stage, 0)

    def bump(self, stage: str) -> int:
        self.attempts[stage] = self.attempts_for(stage) + 1
        return self.attempts[stage]

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


class ScreeningAgent:
    """Generic screening engine: a deterministic stage machine driven by a job
    profile, wrapped around an LLM for language understanding.

    The LLM never decides which question comes next or whether a candidate is
    qualified: it extracts and phrases. Questions, validation, knockout rules,
    flags and scoring come from the session's :class:`JobProfile`, which is
    what makes the pipeline auditable and lets one engine run many recruiting
    processes (couriers, construction labourers, ...).
    """

    def __init__(self, llm: BaseLLM, profiles: Optional[ProfileRegistry] = None):
        self.llm = llm
        self.profiles = profiles or registry()

    def profile(self, state: SessionState) -> JobProfile:
        return self.profiles.get(state.profile_id or None, state.profile_revision or None)

    # ---------- entry points ----------

    def new_session(self, source: str = "web", language: Optional[str] = None,
                    contact: Optional[str] = None, profile_id: Optional[str] = None) -> SessionState:
        profile = self.profiles.get(profile_id)
        state = SessionState(id=str(uuid.uuid4()), profile_id=profile.id, profile_revision=profile.revision,
                             source=source, contact=contact)
        lang = language if language in ("es", "en") else profile.default_language
        state.language = Lang(lang if lang in ("es", "en") else "es")
        return state

    def opening(self, state: SessionState) -> str:
        text = self._question(Stage.INTRO, state, variant=0)
        state.history.append({"role": "assistant", "content": text})
        if Stage.INTRO not in state.completed_stages:
            state.completed_stages.append(Stage.INTRO)
        return text

    def resume(self, state: SessionState) -> str:
        """Reopen an abandoned (silence-timeout) session at the stage the
        candidate dropped. Qualified/disqualified decisions stay final."""
        if state.stage in FINAL_STAGES or state.stage == Stage.VERIFYING:
            raise ValueError("final decisions cannot be resumed")
        profile = self.profile(state)
        dropped = state.dropped_at or (state.summary or {}).get("dropped_at_stage")
        stage = dropped if dropped and (dropped == Stage.INTRO or profile.question(dropped)) else None
        if stage is None or stage == Stage.INTRO and state.data.filled():
            stage = self._next_required_stage(state)
        self._reactivate(state, stage)
        reply = f"{localize(profile.messages.resume, state.language.value)} {self._question(stage, state)}".strip()
        state.history.append({"role": "assistant", "content": reply})
        return reply

    def reopen(self, state: SessionState, question_id: Optional[str] = None) -> str:
        """Recruiter action: reopen any session (even a final decision) and
        re-ask ``question_id`` (its previous answer is cleared). Without a
        question, a knockout is re-asked; otherwise the next missing answer."""
        profile = self.profile(state)
        if question_id is None and state.disqualify_reason:
            rule = profile.rule(state.disqualify_reason)
            if rule is not None:
                q = self._question_for_rule(profile, rule)
                question_id = q.id if q else None
        if question_id is not None:
            q = profile.question(question_id)
            if q is None:
                raise ValueError(f"unknown question {question_id!r}")
            for key in q.keys():
                state.data.pop(key, None)
            for check in profile.checks_for_trigger(q.id):  # a new answer means a new verification
                state.data.pop(check.field, None)
            if q.id in state.completed_stages:
                state.completed_stages.remove(q.id)
        if state.disqualify_reason:
            knocked = profile.rule(state.disqualify_reason)
            stale = {state.disqualify_reason, *(knocked.flags if knocked else [])}
            state.flags = [f for f in state.flags if f not in stale]
        state.disqualify_reason = None
        state.close_reason = None
        stage = question_id or self._next_required_stage(state)
        if stage == Stage.REVIEW:
            stage = profile.questions[-1].id
        self._reactivate(state, stage)
        reply = f"{localize(profile.messages.resume, state.language.value)} {self._question(stage, state)}".strip()
        state.history.append({"role": "assistant", "content": reply})
        return reply

    @staticmethod
    def _question_for_rule(profile: JobProfile, rule: Rule) -> Optional[QuestionSpec]:
        if rule.question:
            return profile.question(rule.question)
        q = profile.question_for_field(rule.field or "")
        if q is None:  # rule on an external check result: re-ask the question that triggered it
            check = next((c for c in profile.checks if c.field == rule.field), None)
            if check is not None:
                q = profile.question(check.trigger)
        return q

    def handback(self, state: SessionState) -> str:
        """End of a human takeover: the agent picks the pending question up again."""
        profile = self.profile(state)
        state.bot_paused = False
        text = localize(profile.messages.handback, state.language.value)
        if state.stage not in TERMINAL_STAGES:
            text = f"{text} {self._question(state.stage, state)}".strip()
        state.history.append({"role": "assistant", "content": text})
        return text

    def reengage_text(self, state: SessionState, attempt: int) -> str:
        profile = self.profile(state)
        messages = profile.messages.reengage.get(str(attempt)) or DEFAULT_REENGAGE.get(str(attempt)) or DEFAULT_REENGAGE["1"]
        return localize(messages, state.language.value)

    def resume_text(self, state: SessionState) -> str:
        return localize(self.profile(state).messages.resume, state.language.value)

    def paused_text(self, state: SessionState) -> str:
        return localize(self.profile(state).messages.paused, state.language.value)

    def end(self, state: SessionState, reason: Optional[str] = None) -> tuple[str, TurnDecision]:
        """Close a session from outside a turn (candidate button, silence timeout)."""
        decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
        profile = self.profile(state)
        stage = Stage.QUALIFIED if reason is None and profile.is_qualified(state.data) else Stage.ABANDONED
        reply = self._close(state, decision, stage, reason, None)
        return reply, decision

    def _reactivate(self, state: SessionState, stage: str) -> None:
        state.stage = stage
        state.status = "active"
        state.summary = None
        state.dropped_at = None
        state.reengage_attempts = 0
        state.last_candidate_ts = datetime.now(timezone.utc)
        state.attempts.pop(stage, None)
        if stage in state.given_up:
            state.given_up.remove(stage)

    def process(self, state: SessionState, user_text: str, today: Optional[date] = None) -> tuple[str, TurnDecision]:
        today = today or date.today()
        profile = self.profile(state)
        decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)

        screened = screen_input(user_text)
        clean = screened.sanitized
        if settings.pii_redaction:
            clean = redact_pii(clean)

        if state.bot_paused:
            return self._listen_only(state, decision, screened.reason, clean), decision

        if state.stage in TERMINAL_STAGES:
            closing = profile.messages.verifying if state.stage == Stage.VERIFYING else profile.messages.review
            reply = localize(closing, state.language.value)
            decision.events.append({"type": "ignored_after_close", "payload": {"outcome": state.stage}})
            return reply, decision

        # redaction placeholders ("[id redacted]") are not the candidate's words
        detected = detect_language(_REDACTED.sub(" ", clean), default=state.language)
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
            state.add_flag("self_harm")
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
            extraction = self.llm.extract(state.history, state.stage, today, profile=profile)
        except LLMUnavailable:
            extraction = ExtractionResult(intent=Intent.UNKNOWN, language=reply_lang)
            state.add_flag("llm_unavailable")
            decision.events.append({"type": "llm_error", "payload": {"stage": state.stage}})

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
            stage = Stage.QUALIFIED if profile.is_qualified(state.data, today) else Stage.ABANDONED
            return self._close(state, decision, stage, None, clean), decision

        if wants_human(clean):
            state.add_flag("requested_human_callback")
            state.notes.append("Candidate asked for a human recruiter.")

        if extraction.intent is Intent.QUESTION:
            return self._answer_question(state, decision, extraction, clean), decision
        if extraction.intent is Intent.OFF_TOPIC:
            decision.guardrail = "off_topic"
            decision.events.append({"type": "guardrail", "payload": {"reason": "off_topic"}})
            return self._guardrail_reply(state, decision, "off_topic", clean), decision

        self._apply_extraction(state, profile, extraction, decision, today, clean, raw=screened.sanitized)
        closing = self._advance(state, profile, decision, today)
        if closing is not None:
            return closing, decision

        plan = self._plan_for(state, decision)
        reply = self._phrase(state, decision, plan, clean)
        self._commit(state, reply, decision)
        return reply, decision

    def _listen_only(self, state: SessionState, decision: TurnDecision, guardrail: str, clean: str) -> str:
        """Human takeover: store the candidate message for the recruiter, the agent stays silent.

        Safety still comes first: a self-harm message gets the helpline answer immediately.
        """
        state.history.append({"role": "user", "content": clean})
        state.last_candidate_ts = datetime.now(timezone.utc)
        decision.paused = True
        decision.events.append({"type": "candidate_message_during_takeover", "payload": {"stage": state.stage}})
        if guardrail == "self_harm":
            state.add_flag("self_harm")
            state.notes.append("Candidate expressed self-harm risk during a recruiter conversation; helpline shared.")
            decision.events.append({"type": "guardrail", "payload": {"reason": "self_harm"}})
            reply = boundary_message("self_harm", state.language.value)
            self._commit(state, reply, decision)
            return reply
        return ""

    # ---------- extraction -> data ----------

    def _apply_extraction(self, state: SessionState, profile: JobProfile, ex: ExtractionResult,
                          decision: TurnDecision, today: date, clean: str, raw: Optional[str] = None) -> None:
        ctx = profile.context(today)
        current = profile.question(state.stage)

        # Candidates often answer several questions at once: accept every valid field,
        # but only the pending question's validation error drives the re-ask.
        for q in profile.questions:
            outcome = q.handler.apply(q, ex.fields, ctx)
            if outcome is None:
                continue
            self._record(state, q, outcome, decision, is_current=q is current)

        # Deterministic raw-text fallback for the pending question (LLM returned nothing usable).
        if current is not None and state.data.is_empty(current.field):
            # sensitive answers (ID numbers) are read from the raw message: the model and the
            # stored transcript only ever see the redacted text
            text = raw if current.handler.sensitive and raw is not None else clean
            parsed = current.handler.parse_text(current, text, ctx)
            outcome = current.handler.apply(current, parsed, ctx) if parsed else None
            if outcome is not None and current.handler.sensitive:
                # for sensitive fields this parser is the only one: its errors are the real answer
                self._record(state, current, outcome, decision, is_current=True)
            elif outcome is not None and outcome.values and outcome.error is None:
                self._record(state, current, outcome, decision, is_current=True)
                decision.validation_error = None
                decision.events.append({"type": "field_fallback", "payload": {"field": current.field, "source": "raw_text"}})
            elif outcome is not None and outcome.values:
                # partial success (e.g. experience tags without years): keep what is valid
                for key, value in outcome.values.items():
                    if state.data.is_empty(key):
                        state.data[key] = value

        if ex.notes:
            state.notes.append(str(ex.notes)[:200])

    def _record(self, state: SessionState, q: QuestionSpec, outcome: FieldOutcome,
                decision: TurnDecision, is_current: bool) -> None:
        decision.events.extend(outcome.events)
        for flag in outcome.flags:
            state.add_flag(flag)
        for key, value in outcome.values.items():
            state.data[key] = value
        decision.secrets.update(outcome.secrets)
        if outcome.values and not outcome.error and q.id not in decision.answered:
            decision.answered.append(q.id)
        if outcome.error:
            if is_current:
                decision.validation_error = outcome.error
                decision.error_params = dict(outcome.error_params)
            elif not any(e.get("type") == "validation_fail" for e in outcome.events):
                decision.events.append({"type": "validation_fail", "payload": {"field": q.field, "error": outcome.error}})

    def _advance(self, state: SessionState, profile: JobProfile, decision: TurnDecision, today: date) -> Optional[str]:
        """Move the state machine forward. Returns a closing reply when the
        screening ends on this turn, otherwise None."""
        current = state.stage
        q = profile.question(current)
        error = decision.validation_error

        knockout = profile.knockout(state.data, today)
        if knockout is not None:
            decision.events.append({"type": "rule_triggered", "payload": {"rule": knockout.id, "action": "disqualify"}})
            return self._close(state, decision, Stage.DISQUALIFIED, knockout.id, None, rule=knockout)

        if error and q is not None:
            for rule in profile.rules:
                if rule.on_error != error or rule.question != q.id:
                    continue
                if state.attempts_for(q.id) < rule.after_attempts:
                    continue
                for flag in rule.flags:
                    state.add_flag(flag)
                decision.events.append({"type": "rule_triggered", "payload": {"rule": rule.id, "action": rule.action}})
                if rule.action == "disqualify":
                    return self._close(state, decision, Stage.DISQUALIFIED, rule.id, None, rule=rule)
                self._flag(state, rule)

        self._trigger_checks(state, profile, decision, today, decision.answered)
        self._apply_flag_rules(state, profile, decision, today)

        still_missing = q is not None and state.data.is_empty(q.field)
        if error is None and still_missing:
            error = q.handler.empty_error(q)
            decision.validation_error = error

        if q is not None and error and q.required:
            attempts = state.bump(current)
            if attempts >= profile.max_stage_attempts:
                state.given_up.append(current)
                state.add_flag(f"unresolved:{current}")
                state.notes.append(f"Could not validate {q.field} after {attempts} attempts; moved on with a flag.")
                decision.events.append({"type": "give_up_field", "payload": {"stage": current, "attempts": attempts}})
            else:
                decision.stage_after = current
                return None

        if q is not None and not q.required and error:
            state.bump(current)
            state.given_up.append(current)
            state.add_flag(f"unresolved:{current}")
            decision.validation_error = None
            decision.events.append({"type": "give_up_field", "payload": {"stage": current, "attempts": state.attempts_for(current)}})

        target = self._next_required_stage(state)

        if target == Stage.REVIEW:
            if current not in state.completed_stages:
                state.completed_stages.append(current)
            self._trigger_checks(state, profile, decision, today, [ON_COMPLETE])
            if profile.is_qualified(state.data, today):
                if profile.pending_blocking_checks(state.data):
                    return self._close(state, decision, Stage.VERIFYING, None, None)
                return self._close(state, decision, Stage.QUALIFIED, None, None)
            state.add_flag("needs_review")
            state.notes.append(
                "All questions were asked but some required fields could not be validated; recruiter review needed."
            )
            decision.events.append({"type": "needs_review", "payload": {"missing": profile.missing_required(state.data)}})
            return self._close(state, decision, Stage.ABANDONED, None, None)

        if target != current:
            if current not in state.completed_stages:
                state.completed_stages.append(current)
            decision.advanced = True
            decision.events.append({"type": "stage_advance", "payload": {"from": current, "to": target}})
        state.stage = target
        return None

    # ---------- external checks ----------

    def _trigger_checks(self, state: SessionState, profile: JobProfile, decision: TurnDecision, today: date,
                        triggers: list[str]) -> None:
        """Queue the external checks fired by this turn (the app dispatches them after persisting)."""
        for trigger in triggers:
            for check in profile.checks_for_trigger(trigger):
                if trigger == ON_COMPLETE and state.data.get(check.field) not in (None, ""):
                    continue  # already requested / resolved
                if check.when is not None and not check.when.matches(state.data, today):
                    continue
                payload = {key: decision.secrets.get(key, state.data.get(key)) for key in check.inputs}
                state.data[check.field] = "pending"
                decision.check_requests.append({"check": check.id, "payload": payload})
                decision.events.append({"type": "check_requested",
                                        "payload": {"check": check.id, "provider": check.provider,
                                                    "blocking": check.blocking}})

    def apply_check_result(self, state: SessionState, check_id: str, result: str,
                           detail: Optional[str] = None, today: Optional[date] = None) -> tuple[Optional[str], TurnDecision]:
        """An external check finished (asynchronously): store the result and re-run the rules.

        Returns the message to deliver to the candidate (knockout, final decision) or None.
        """
        today = today or date.today()
        profile = self.profile(state)
        decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
        check = profile.check(check_id)
        if check is None or result not in ("valid", "invalid", "error"):
            raise ValueError(f"unknown check {check_id!r} or result {result!r}")
        state.data[check.field] = result
        decision.events.append({"type": "check_result", "payload": {"check": check_id, "result": result,
                                                                    "detail": (detail or "")[:200]}})
        if result == "error":
            state.add_flag(f"check_error:{check_id}")
            state.notes.append(f"External check {check_id} failed or timed out; verify manually.")
        else:  # a definitive answer (e.g. a recruiter's manual decision) resolves an earlier failure
            state.flags = [f for f in state.flags if f != f"check_error:{check_id}"]
        if state.stage == Stage.DISQUALIFIED:
            return None, decision  # a negative decision stays final

        knockout = profile.knockout(state.data, today)
        if knockout is not None:
            decision.events.append({"type": "rule_triggered", "payload": {"rule": knockout.id, "action": "disqualify"}})
            return self._close(state, decision, Stage.DISQUALIFIED, knockout.id, None, rule=knockout), decision
        self._apply_flag_rules(state, profile, decision, today)

        if state.stage == Stage.VERIFYING and not profile.pending_blocking_checks(state.data):
            if profile.is_qualified(state.data, today):
                return self._close(state, decision, Stage.QUALIFIED, None, None), decision
            state.add_flag("needs_review")
            return self._close(state, decision, Stage.ABANDONED, None, None), decision
        decision.stage_after = state.stage
        return None, decision

    def _apply_flag_rules(self, state: SessionState, profile: JobProfile, decision: TurnDecision, today: date) -> None:
        for rule in profile.rules:
            if rule.action != "flag" or not rule.field:
                continue
            if rule.matches(state.data, today) and not self._flagged(state, rule):
                self._flag(state, rule)
                decision.events.append({"type": "rule_triggered", "payload": {"rule": rule.id, "action": "flag"}})

    @staticmethod
    def _flagged(state: SessionState, rule: Rule) -> bool:
        return all(f in state.flags for f in (rule.flags or [rule.id]))

    @staticmethod
    def _flag(state: SessionState, rule: Rule) -> None:
        for flag in rule.flags or [rule.id]:
            state.add_flag(flag)
        if rule.note and rule.note not in state.notes:
            state.notes.append(rule.note)

    def _next_required_stage(self, state: SessionState) -> str:
        profile = self.profile(state)
        for q in profile.questions:
            if q.id in state.given_up:
                continue
            if state.data.is_empty(q.field):
                return q.id
        return Stage.REVIEW

    # ---------- replies ----------

    def _answer_question(self, state: SessionState, decision: TurnDecision, ex: ExtractionResult, clean: str) -> str:
        profile = self.profile(state)
        question = ex.candidate_question or clean
        pending = self._question(state.stage, state)
        docs = profile.faq_index.search(question, lang=state.language.value, k=2) if profile.faq_index else []
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
        if state.stage in TERMINAL_STAGES:
            return ""
        question = self._question(state.stage, state)
        if decision.validation_error:
            return f"The previous answer was not valid ({decision.validation_error}). Say: {self._template_reply(state, decision)}"
        tone = ""
        if decision.sentiment in (Sentiment.FRUSTRATED, Sentiment.CONFUSED):
            tone = "The candidate sounds frustrated or confused: be shorter and warmer, and offer a recruiter callback."
        return f"Ask the candidate the next screening question. {tone}\nQuestion to ask: {question}"

    def _template_reply(self, state: SessionState, decision: TurnDecision) -> str:
        error = decision.validation_error
        if not error:
            return self._question(state.stage, state)
        profile = self.profile(state)
        lang = state.language.value
        q = profile.question(state.stage)
        template = (q.reask.get(error) if q else None) or DEFAULT_REASK.get(error) or DEFAULT_REASK["empty"]
        question = self._question(state.stage, state)
        params = decision.error_params or {}
        fallback_value = "esa ciudad" if lang == "es" else "that city"
        return _fmt(
            localize(template, lang),
            question=question,
            next=question,
            options=params.get("options") or (q.options_text(lang) if q else ""),
            value=params.get("value") or fallback_value,
            city=params.get("value") or fallback_value,
        )

    def _phrase(self, state: SessionState, decision: TurnDecision, plan: str, clean: str,
                fallback: Optional[str] = None, faq_block: str = "") -> str:
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
                profile=self.profile(state),
            )
        except LLMUnavailable:
            state.add_flag("llm_unavailable")
            return fallback or self._template_reply(state, decision)
        if any(marker in reply for marker in _LEAK_MARKERS):
            decision.events.append({"type": "phrasing_fallback", "payload": {"reason": "prompt_leak"}})
            return fallback or self._template_reply(state, decision)
        return reply

    def _commit(self, state: SessionState, reply: str, decision: TurnDecision) -> None:
        state.history.append({"role": "assistant", "content": reply})
        decision.stage_after = state.stage

    def _close(self, state: SessionState, decision: TurnDecision, stage: str, reason: Optional[str],
               clean: Optional[str], override: Optional[str] = None, rule: Optional[Rule] = None) -> str:
        profile = self.profile(state)
        lang = state.language.value
        if state.stage not in TERMINAL_STAGES:
            state.dropped_at = state.stage
        state.stage = stage
        state.status = stage
        decision.stage_after = stage
        state.close_reason = reason
        if reason:
            decision.disqualify_reason = reason
            state.add_flag(reason)
        if stage == Stage.DISQUALIFIED:
            state.disqualify_reason = reason
            rule = rule or profile.rule(reason)
        if stage == Stage.QUALIFIED and Stage.REVIEW not in state.completed_stages:
            state.completed_stages.append(Stage.REVIEW)

        if override:
            fallback = override
        elif stage == Stage.DISQUALIFIED and rule is not None and rule.message:
            fallback = localize(rule.message, lang)
        elif stage == Stage.QUALIFIED:
            fallback = localize(profile.messages.qualified, lang)
        elif stage == Stage.VERIFYING:
            fallback = localize(profile.messages.verifying, lang)
        else:
            fallback = localize(profile.messages.review, lang)

        plan = f"The screening is finished with outcome {stage}. Close the conversation warmly and state the next step. Say: {fallback}"
        reply = self._phrase(state, decision, plan, clean or "", fallback=fallback)
        state.history.append({"role": "assistant", "content": reply})
        decision.events.append({"type": "session_close", "payload": {"outcome": stage, "reason": reason}})
        state.summary = self.summarize(state).model_dump(mode="json")
        return reply

    # ---------- summary ----------

    def summarize(self, state: SessionState) -> SessionSummary:
        profile = self.profile(state)
        duration = int((datetime.now(timezone.utc) - state.started_at).total_seconds())
        candidate_turns = sum(1 for m in state.history if m.get("role") == "user")
        outcome = state.stage
        if outcome in FINAL_STAGES or outcome == Stage.VERIFYING or "needs_review" in state.flags:
            dropped = None
        else:
            dropped = state.dropped_at or outcome
        score, _ = profile.score(state.data)
        summary = SessionSummary(
            profile_id=profile.id,
            qualified=outcome == Stage.QUALIFIED,
            outcome=outcome,
            disqualify_reason=state.disqualify_reason if outcome == Stage.DISQUALIFIED else None,
            close_reason=state.close_reason,
            completed_stages=list(state.completed_stages),
            dropped_at_stage=dropped,
            duration_seconds=duration,
            candidate_turns=candidate_turns,
            languages=list(state.languages_seen),
            data=profile.complete_data(state.data),
            score=score,
            recruiter_notes=list(state.notes),
            flags=list(state.flags),
        )
        if summary.qualified and self.llm.available():
            note = self._recruiter_note(summary, profile)
            if note:
                summary.recruiter_notes.append(note)
        return summary

    def _recruiter_note(self, summary: SessionSummary, profile: JobProfile) -> str:
        language = summary.languages[0] if summary.languages else profile.default_language
        compact = {k: v for k, v in summary.data.items() if v not in (None, [], "")}
        try:
            note = self.llm.generate(
                stage=Stage.REVIEW,
                language=language,
                sentiment=Sentiment.NEUTRAL,
                plan=SUMMARY_NOTE_PROMPT.format(language=language, role=profile.role_name("en"), data=compact),
                faq_block="",
                candidate_message="",
                state="",
                profile=profile,
            )
        except LLMUnavailable:
            return ""
        return note[:200]

    # ---------- helpers ----------

    def _question(self, stage: str, state: SessionState, variant: Optional[int] = None) -> str:
        profile = self.profile(state)
        lang = state.language.value
        if stage == Stage.INTRO:
            variants = localize(profile.intro, lang, [])
        else:
            q = profile.question(stage)
            if q is None:
                return ""
            variants = localize(q.prompts, lang, [])
        if not variants:
            return ""
        idx = variant if variant is not None else state.attempts_for(stage) % len(variants)
        return _fmt(variants[idx % len(variants)], name=profile.persona.agent_name, company=profile.company,
                    role=profile.role_name(lang))

    def question_text(self, state: SessionState) -> str:
        """The pending question, phrased for the session's language."""
        return self._question(state.stage, state)

    def _state_digest(self, state: SessionState) -> str:
        return str(state.data.filled())[:600]
