from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from ..screening.schema import FLOW
from ..storage import db


def _pct(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def compute(sessions: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Compute recruiter-facing analytics from stored sessions."""
    if sessions is None:
        sessions = db.list_sessions(limit=1000)

    total = len(sessions)
    active = [s for s in sessions if s["status"] == "active"]
    closed = [s for s in sessions if s["status"] != "active"]
    qualified = [s for s in closed if s["qualified"]]
    disqualified = [s for s in closed if s["status"] == "disqualified"]
    abandoned = [s for s in closed if s["status"] == "abandoned"]

    durations = [s["duration_seconds"] for s in closed if s["duration_seconds"] is not None]
    turns = [s["candidate_turns"] for s in closed]

    dropoffs = Counter(s["dropped_at_stage"] for s in abandoned if s["dropped_at_stage"])
    reasons = Counter(s["disqualify_reason"] for s in disqualified if s["disqualify_reason"])

    completed_sets = [_completed(s) for s in sessions]
    funnel: dict[str, int] = {}
    for stage in FLOW:
        funnel[stage.value] = sum(1 for done in completed_sets if stage.value in done)
    funnel["qualified"] = len(qualified)

    languages = Counter()
    for s in sessions:
        for lang in s.get("languages") or []:
            languages[lang] += 1

    reengaged = [s for s in sessions if s["reengage_attempts"] > 0]
    reengaged_recovered = [s for s in reengaged if s["status"] in ("qualified", "disqualified")]

    rag_counter: Counter[str] = Counter()
    for s in sessions:
        rag_counter.update(s.get("rag_hits") or [])

    flags = Counter()
    for s in sessions:
        for flag in s.get("flags") or []:
            flags[flag.split(":")[0]] += 1

    return {
        "total_sessions": total,
        "active_sessions": len(active),
        "closed_sessions": len(closed),
        "completion_rate_pct": _pct(len(closed), total),
        "qualified_count": len(qualified),
        "qualified_rate_pct": _pct(len(qualified), total),
        "disqualified_count": len(disqualified),
        "abandoned_count": len(abandoned),
        "avg_duration_seconds": round(sum(durations) / len(durations)) if durations else 0,
        "avg_candidate_turns": round(sum(turns) / len(turns), 1) if turns else 0,
        "dropoff_by_stage": dict(dropoffs.most_common()),
        "disqualify_reasons": dict(reasons.most_common()),
        "funnel": funnel,
        "languages": dict(languages),
        "reengagement": {
            "nudged_sessions": len(reengaged),
            "recovered_sessions": len(reengaged_recovered),
            "recovery_rate_pct": _pct(len(reengaged_recovered), len(reengaged)),
        },
        "top_rag_hits": dict(rag_counter.most_common(5)),
        "flag_counts": dict(flags.most_common(10)),
    }


def _completed(session: dict[str, Any]) -> list[str]:
    summary = session.get("summary") or {}
    return summary.get("completed_stages") or []
