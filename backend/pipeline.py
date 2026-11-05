"""Recruiter pipeline: the hiring stages a candidate moves through after (and
during) the automated screening.

Stages are configurable in the backoffice (``pipeline_stages`` table): recruiters
rename, recolour, reorder, add and delete them. Four **system** stages are the
targets of automatic placement and can be renamed but not deleted.

The screening outcome (``status``) is decided by the agent; the pipeline stage
is owned by recruiters. Until a recruiter moves a candidate manually, the
pipeline stage follows the screening outcome automatically (``auto_stage``).
"""

from __future__ import annotations

from typing import Iterable

SYSTEM_STAGES = ("screening", "review", "qualified", "rejected")
STAGE_COLORS = ("gray", "blue", "indigo", "violet", "pink", "red", "orange", "amber", "green", "teal")

DEFAULT_STAGES: list[dict] = [
    {"id": "screening", "label": "En screening", "color": "gray"},
    {"id": "review", "label": "Por revisar", "color": "amber"},
    {"id": "qualified", "label": "Pre-calificado", "color": "teal"},
    {"id": "shortlisted", "label": "Preseleccionado", "color": "indigo"},
    {"id": "interview", "label": "Entrevista", "color": "blue"},
    {"id": "offer", "label": "Oferta", "color": "violet"},
    {"id": "hired", "label": "Contratado", "color": "green"},
    {"id": "rejected", "label": "Descartado", "color": "red"},
]

# Flags that always deserve a human look before a candidate is shortlisted.
SYSTEM_REVIEW_FLAGS = frozenset({"self_harm"})


def auto_stage(status: str, flags: Iterable[str] = (), review_flags: Iterable[str] = ()) -> str:
    """Pipeline stage implied by the screening outcome.

    ``review_flags`` are the profile's flag rules marked ``review: true`` (e.g. a
    construction candidate who reported physical limitations): a qualified
    candidate carrying one goes to "review" instead of "qualified".
    """
    flags = set(flags or [])
    if status in ("active", "verifying"):  # verifying: answers complete, external checks still running
        return "screening"
    if status == "qualified":
        if flags & (set(review_flags or ()) | SYSTEM_REVIEW_FLAGS) or any(f.startswith("check_error:") for f in flags):
            return "review"
        return "qualified"
    if status == "disqualified":
        return "rejected"
    # abandoned: incomplete screenings are worth a recruiter call, abusive ones are not
    if "abusive_language" in flags:
        return "rejected"
    return "review"
