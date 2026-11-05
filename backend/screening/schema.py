from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Stage:
    """Lifecycle stages shared by every screening profile.

    Question stages are *not* listed here: they are the question ids declared
    by each job profile (e.g. ``license`` for couriers, ``work_permit`` for
    construction). A session's ``stage`` is therefore always a plain string:
    one of these lifecycle constants or a question id of its profile.
    """

    INTRO = "intro"
    REVIEW = "review"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    ABANDONED = "abandoned"
    # all questions answered, final decision waits for blocking external checks
    VERIFYING = "verifying"


# no more questions are asked in these stages
TERMINAL_STAGES = frozenset({Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED, Stage.VERIFYING})
FINAL_STAGES = frozenset({Stage.QUALIFIED, Stage.DISQUALIFIED})


class Intent(str, Enum):
    ANSWER = "answer"
    QUESTION = "question"
    OFF_TOPIC = "off_topic"
    ABUSE = "abuse"
    INJECTION = "injection"
    END = "end"
    UNKNOWN = "unknown"


class Sentiment(str, Enum):
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    FRUSTRATED = "frustrated"
    CONFUSED = "confused"


class Lang(str, Enum):
    ES = "es"
    EN = "en"
    MIXED = "mixed"


class CandidateData(dict):
    """Structured answers collected by the screening, keyed by profile field.

    A plain JSON-serialisable dict (dates are ISO strings, choices are option
    values) with attribute access for readability: ``data.city`` is
    ``data.get("city")``. Which keys exist is defined by the job profile.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def is_empty(self, key: str) -> bool:
        return self.get(key) in (None, [], "")

    def filled(self) -> dict[str, Any]:
        return {k: v for k, v in self.items() if v not in (None, [], "")}


META_KEYS = ("intent", "language", "sentiment", "candidate_question", "notes")


class ExtractionResult(BaseModel):
    """What the LLM returns for a single candidate message.

    ``fields`` holds raw values keyed by profile field name (and auxiliary keys
    such as ``zone`` for locations). They are validated by the field handlers,
    never trusted as-is.
    """

    intent: Intent = Intent.UNKNOWN
    language: Lang = Lang.ES
    sentiment: Sentiment = Sentiment.NEUTRAL
    candidate_question: Optional[str] = None
    notes: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class TurnDecision(BaseModel):
    """Deterministic outcome of processing one candidate message."""

    stage_before: str
    stage_after: str
    advanced: bool = False
    validation_error: Optional[str] = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    guardrail: Optional[str] = None
    disqualify_reason: Optional[str] = None
    rag_used: list[str] = Field(default_factory=list)
    language: Lang = Lang.ES
    sentiment: Sentiment = Sentiment.NEUTRAL
    paused: bool = False
    events: list[dict[str, Any]] = Field(default_factory=list)
    # questions validly answered this turn (check triggers)
    answered: list[str] = Field(default_factory=list)
    # external checks to dispatch: [{"check": id, "payload": {...}}]. May contain sensitive raw
    # values (a full ID number): never serialised, never stored in events.
    check_requests: list[dict[str, Any]] = Field(default_factory=list, exclude=True, repr=False)
    secrets: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


class SessionSummary(BaseModel):
    """Recruiter/ATS contract produced when a screening closes."""

    profile_id: str
    qualified: bool
    outcome: str
    disqualify_reason: Optional[str] = None
    close_reason: Optional[str] = None
    completed_stages: list[str] = Field(default_factory=list)
    dropped_at_stage: Optional[str] = None
    duration_seconds: int = 0
    candidate_turns: int = 0
    languages: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    score: Optional[float] = None
    recruiter_notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
