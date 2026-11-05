"""Screening results for recruiters: per-question answers, rule evaluation and score breakdown."""

from __future__ import annotations

from typing import Any, Optional

from ..screening.profile import JobProfile, Rule, localize
from .labels import CHECK_STATUS_LABELS, PROVIDER_LABELS, flag_label


_OPS = {"eq": "=", "ne": "≠", "lt": "<", "lte": "≤", "gt": ">", "gte": "≥", "in": "∈", "not_in": "∉",
        "contains": "incluye", "not_contains": "no incluye", "missing": "sin respuesta", "present": "respondida",
        "within_days": "en ≤ días"}
_ERRORS = {"out_of_area": "fuera de la zona de cobertura", "ambiguous_city": "ciudad ambigua"}


def describe_condition(profile: JobProfile, rule: Rule, lang: str = "es") -> str:
    """Recruiter-readable rule condition, e.g. "Mayor de edad = No"."""
    if rule.on_error:
        q = profile.question(rule.question)
        label = localize(q.label, lang, rule.question) if q else rule.question
        retries = f" (tras {rule.after_attempts} reintento{'s' if rule.after_attempts != 1 else ''})" if rule.after_attempts else ""
        return f"{label}: {_ERRORS.get(rule.on_error, rule.on_error)}{retries}"
    q = profile.question_for_field(rule.field or "")
    label = localize(q.label, lang, rule.field) if q and q.field == rule.field else (rule.field or "")
    check = next((c for c in profile.checks if c.field == rule.field), None)
    if check is not None:
        label = localize(check.label, lang, check.id)

    def show(value: Any) -> str:
        if check is not None and isinstance(value, str):
            return localize(CHECK_STATUS_LABELS.get(value), lang, value).lower()
        if value is True:
            return "Sí" if lang == "es" else "Yes"
        if value is False:
            return "No"
        if isinstance(value, list):
            return ", ".join(show(v) for v in value)
        if q is not None and q.options:
            return q.option_label(str(value), lang)
        return str(value)

    if rule.op in ("missing", "present"):
        return f"{label}: {_OPS[rule.op]}"
    return f"{label} {_OPS.get(rule.op, rule.op)} {show(rule.value)}"


def build_checks(profile: JobProfile, session: dict[str, Any], verifications: list[dict[str, Any]],
                 lang: str = "es") -> list[dict[str, Any]]:
    """One row per external check of the profile: its latest verification and the available actions."""
    data = session.get("data") or {}
    out = []
    for check in profile.checks:
        rows = [v for v in verifications if v["check_id"] == check.id and v["status"] != "superseded"]
        latest = rows[-1] if rows else None
        value = data.get(check.field)
        status = latest["status"] if latest else ("pending" if value == "pending" else value or "not_requested")
        if status in ("queued", "submitted"):
            status = "pending"
        trigger = profile.question(check.trigger)
        out.append({
            "id": check.id,
            "label": localize(check.label, lang, check.id),
            "provider": check.provider,
            "provider_label": localize(PROVIDER_LABELS.get(check.provider), lang, check.provider),
            "blocking": check.blocking,
            "trigger": localize(trigger.label, lang, check.trigger) if trigger else "Al completar el screening",
            "field": check.field,
            "status": status,
            "status_label": localize(CHECK_STATUS_LABELS.get(status), lang, status),
            "verification": latest,
            "attempts_total": len(rows),
            "can_decide": latest is not None,
            "can_retry": bool(latest and latest["status"] == "error" and latest.get("has_payload")),
        })
    return out


def build_report(profile: JobProfile, session: dict[str, Any], lang: str = "es",
                 verifications: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    data = session.get("data") or {}
    flags = session.get("flags") or []
    given_up = session.get("given_up") or []
    attempts = session.get("attempts") or {}
    status = session.get("status")
    stage = session.get("stage")
    disqualify_reason = session.get("disqualify_reason")
    messages = session.get("messages") or []

    knockout = profile.rule(disqualify_reason) if status == "disqualified" else None
    knockout_fields = set()
    if knockout is not None:
        if knockout.field:
            knockout_fields.add(knockout.field)
        if knockout.question:
            q = profile.question(knockout.question)
            if q:
                knockout_fields.add(q.field)

    answers = []
    for q in profile.questions:
        value = data.get(q.field)
        answered = value not in (None, [], "")
        if q.field in knockout_fields:
            state = "knockout"
        elif answered:
            state = "answered"
        elif q.id in given_up:
            state = "unresolved"
        elif status == "active" and stage == q.id:
            state = "pending"
        else:
            state = "missing"
        raw = [m["content"] for m in messages if m.get("role") == "user" and m.get("stage") == q.id]
        answers.append({
            "id": q.id,
            "field": q.field,
            "type": q.type,
            "label": localize(q.label, lang, q.id),
            "question": localize(q.prompts, lang, [""])[0],
            "required": q.required,
            "value": {k: data.get(k) for k in q.keys() if data.get(k) not in (None, [], "")},
            "display": q.handler.display(q, data, lang),
            "status": state,
            "attempts": attempts.get(q.id, 0),
            "raw_answers": raw[-3:],
        })

    rules = []
    for rule in profile.rules:
        check_fields = {c.field for c in profile.checks}
        if rule.field:
            actual = data.get(rule.field)
            if rule.field in check_fields and actual == "pending":
                result = "pending"  # waiting for the external check
            elif actual in (None, [], "") and rule.op not in ("missing", "present"):
                result = "not_evaluated"
            else:
                result = "triggered" if rule.matches(data) else "passed"
        else:
            hit = rule.id == disqualify_reason or any(f in flags for f in rule.flags)
            q = profile.question(rule.question)
            answered = q is not None and data.get(q.field) not in (None, [], "")
            result = "triggered" if hit else ("passed" if answered else "not_evaluated")
        condition = describe_condition(profile, rule, lang)
        rules.append({
            "id": rule.id,
            "action": rule.action,
            "label": localize(rule.label, lang, rule.id),
            "condition": condition,
            "result": result,
        })

    score, breakdown = profile.score(data)
    for item in breakdown:
        item["label"] = localize(item["label"], lang, item["field"])

    answered_required = sum(1 for a in answers if a["required"] and a["status"] in ("answered",))
    required_total = sum(1 for a in answers if a["required"])
    summary = session.get("summary") or {}
    return {
        "profile_id": profile.id,
        "outcome": {
            "status": status,
            "qualified": bool(session.get("qualified")),
            "disqualify_reason": disqualify_reason,
            "disqualify_label": localize(knockout.label, lang, disqualify_reason) if knockout else None,
            "close_reason": summary.get("close_reason"),
        },
        "answers": answers,
        "rules": rules,
        "score": score,
        "score_breakdown": breakdown,
        "flags": [{"id": f, "label": flag_label(f, profile, lang)} for f in flags],
        "missing_required": profile.missing_required(data),
        "completion": {"answered": answered_required, "required": required_total},
        "agent_notes": session.get("notes") or [],
        "recruiter_summary": (summary.get("recruiter_notes") or [None])[-1] if summary.get("qualified") else None,
        "rag_hits": session.get("rag_hits") or [],
        "checks": build_checks(profile, session, verifications or [], lang),
    }
