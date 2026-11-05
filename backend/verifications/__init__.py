"""Asynchronous external checks (ID registries, webhooks, manual recruiter checks)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..screening.agent import SessionState
    from ..screening.schema import TurnDecision
    from .service import VerificationService

_SERVICE: Optional["VerificationService"] = None


def set_service(service: Optional["VerificationService"]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional["VerificationService"]:
    return _SERVICE


def dispatch_requests(state: "SessionState", decision: "TurnDecision") -> None:
    """Queue the checks requested by a turn (no-op when the app runs without the worker)."""
    if _SERVICE is not None and decision.check_requests:
        _SERVICE.request_from_decision(state, decision)
