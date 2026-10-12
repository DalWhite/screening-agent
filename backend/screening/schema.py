from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Stage(str, Enum):
    INTRO = "intro"
    NAME = "name"
    LICENSE = "license"
    LOCATION = "location"
    AVAILABILITY = "availability"
    SCHEDULE = "schedule"
    EXPERIENCE = "experience"
    START_DATE = "start_date"
    REVIEW = "review"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    ABANDONED = "abandoned"


TERMINAL_STAGES = {Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED}

# Order in which required fields are collected.
FLOW: list[Stage] = [
    Stage.INTRO,
    Stage.NAME,
    Stage.LICENSE,
    Stage.LOCATION,
    Stage.AVAILABILITY,
    Stage.SCHEDULE,
    Stage.EXPERIENCE,
    Stage.START_DATE,
    Stage.REVIEW,
]

NEXT_STAGE: dict[Stage, Stage] = {
    FLOW[i]: FLOW[i + 1] for i in range(len(FLOW) - 1)
} | {Stage.REVIEW: Stage.REVIEW}


class Availability(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    WEEKENDS = "weekends"


class Schedule(str, Enum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    FLEXIBLE = "flexible"


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


class ScreeningData(BaseModel):
    """Structured output of the screening. Every field maps to the client brief."""

    full_name: Optional[str] = None
    has_license: Optional[bool] = None
    city: Optional[str] = None
    zone: Optional[str] = None
    country: Optional[str] = None
    in_service_area: Optional[bool] = None
    availability: list[Availability] = Field(default_factory=list)
    preferred_schedule: Optional[Schedule] = None
    experience_years: Optional[int] = None
    experience_platforms: list[str] = Field(default_factory=list)
    start_date: Optional[date] = None

    @field_validator("experience_years")
    @classmethod
    def _years_range(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        return max(0, min(int(v), 50))

    def missing_required(self) -> list[str]:
        out: list[str] = []
        if not self.full_name:
            out.append("full_name")
        if self.has_license is None:
            out.append("has_license")
        if not self.city:
            out.append("city")
        if not self.availability:
            out.append("availability")
        if self.preferred_schedule is None:
            out.append("preferred_schedule")
        if self.start_date is None:
            out.append("start_date")
        return out

    def is_qualified(self) -> bool:
        if self.has_license is False:
            return False
        if self.in_service_area is False:
            return False
        return not self.missing_required()


class ExtractionResult(BaseModel):
    """What the LLM returns for a single candidate message."""

    intent: Intent = Intent.UNKNOWN
    language: Lang = Lang.ES
    sentiment: Sentiment = Sentiment.NEUTRAL
    full_name: Optional[str] = None
    has_license: Optional[bool] = None
    city: Optional[str] = None
    zone: Optional[str] = None
    availability: Optional[list[str]] = None
    preferred_schedule: Optional[str] = None
    experience_years: Optional[int] = None
    experience_platforms: Optional[list[str]] = None
    start_date: Optional[str] = None
    candidate_question: Optional[str] = None
    notes: Optional[str] = None


class TurnDecision(BaseModel):
    """Deterministic outcome of processing one candidate message."""

    stage_before: Stage
    stage_after: Stage
    advanced: bool = False
    validation_error: Optional[str] = None
    guardrail: Optional[str] = None
    disqualify_reason: Optional[str] = None
    rag_used: list[str] = Field(default_factory=list)
    language: Lang = Lang.ES
    sentiment: Sentiment = Sentiment.NEUTRAL
    events: list[dict[str, Any]] = Field(default_factory=list)


class SessionSummary(BaseModel):
    qualified: bool
    outcome: str
    disqualify_reason: Optional[str] = None
    completed_stages: list[str] = Field(default_factory=list)
    dropped_at_stage: Optional[str] = None
    duration_seconds: int = 0
    candidate_turns: int = 0
    languages: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    recruiter_notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
