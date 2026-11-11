"""The candidate session API: the web channel's transport.

Used by the browser chat (``frontend/static/chat.js``) and by integrations that
relay their own channel (an ATS: ``source="ats"``), see docs/ats_integration_api.md.
The conversation logic is `WebChannel` (``backend/channels/web.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..backoffice.auth import require_recruiter
from ..channels.web import DecisionIsFinal, ResumeWindowExpired, SessionNotFound, UnknownProcess, WebChannel
from ..screening.profile import registry as profile_registry
from ..storage import db

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# Recruiter-only columns never leave through the candidate-facing session endpoint.
_RECRUITER_ONLY = ("assignee", "tags", "interview_at", "pipeline_stage", "pipeline_manual", "score", "state",
                   "attempts", "abuse_count")


class CreateSessionRequest(BaseModel):
    source: str = Field(default="web", pattern=r"^[a-z_]{1,20}$")
    language: str | None = None
    profile_id: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def web(request: Request) -> WebChannel:
    return request.app.state.channels.web


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except UnknownProcess as exc:
        raise HTTPException(status_code=404, detail="unknown or inactive profile") from exc
    except ResumeWindowExpired as exc:
        raise HTTPException(status_code=410, detail="resume window expired; start a new conversation") from exc
    except DecisionIsFinal as exc:
        raise HTTPException(status_code=409, detail="final decisions cannot be resumed") from exc


@router.post("")
def create_session(req: CreateSessionRequest, channel: WebChannel = Depends(web)) -> dict:
    state, reply = _call(channel.start, source=req.source, language=req.language, profile_id=req.profile_id)
    return {"session_id": state.id, "reply": reply, "stage": state.stage, "language": state.language.value,
            "profile_id": state.profile_id}


@router.post("/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest, channel: WebChannel = Depends(web)) -> dict:
    return _call(channel.message, session_id, req.text)


@router.get("/{session_id}/pending")
def pending(session_id: str, channel: WebChannel = Depends(web)) -> dict:
    """Queued outbound messages (nudges, recruiter messages) plus the live session status."""
    return _call(channel.pending, session_id)


@router.post("/{session_id}/resume")
def resume_session(session_id: str, channel: WebChannel = Depends(web)) -> dict:
    return _call(channel.resume, session_id)


@router.post("/{session_id}/end")
def end_session(session_id: str, channel: WebChannel = Depends(web)) -> dict:
    return _call(channel.end, session_id)


@router.get("/{session_id}")
def get_session(session_id: str) -> dict:
    """Candidate-safe transcript + summary (no recruiter data)."""
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    for key in _RECRUITER_ONLY:
        session.pop(key, None)
    try:
        profile = profile_registry().get(session["profile_id"])
        session["profile"] = {"id": profile.id, "company": profile.company, "role": profile.role,
                              "agent_name": profile.persona.agent_name}
    except KeyError:
        session["profile"] = None
    return session


@router.get("")
def list_sessions(
    status: str | None = Query(None),
    qualified: bool | None = Query(None),
    q: str | None = Query(None),
    profile_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: str = Depends(require_recruiter),
) -> dict:
    """Recruiter list (ATS integrations)."""
    rows = db.list_sessions(status=status, qualified=qualified, q=q, limit=limit, profile_id=profile_id)
    return {"sessions": rows}
