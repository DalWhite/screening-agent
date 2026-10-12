from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import metrics
from .config import settings
from .llm.client import build_llm
from .reengagement import ReengagementMonitor
from .screening.agent import ScreeningAgent, SessionState
from .screening.guardrails import redact_pii, screen_input
from .screening.schema import Lang, ScreeningData, Stage, TurnDecision
from .storage import db

log = logging.getLogger("sazon.app")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

agent: ScreeningAgent | None = None
registry: dict[str, SessionState] = {}
monitor: ReengagementMonitor | None = None


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_active_sessions() -> None:
    """Restore active sessions from SQLite so restarts don't orphan candidates."""
    for row in db.list_sessions(status="active", limit=500):
        try:
            data = ScreeningData(**(row.get("data") or {}))
        except Exception:
            continue
        state = SessionState(
            id=row["id"],
            stage=Stage(row["stage"]),
            data=data,
            language=Lang(row["language"]),
            status=row["status"],
            source=row["source"],
            started_at=_parse_ts(row["started_at"]),
            reengage_attempts=row["reengage_attempts"],
            flags=list(row.get("flags") or []),
            notes=list(row.get("notes") or []),
            given_up=list(row.get("given_up") or []),
            languages_seen=list(row.get("languages") or []),
            rag_hits=list(row.get("rag_hits") or []),
            summary=row.get("summary"),
            dropped_at=(row.get("summary") or {}).get("dropped_at_stage"),
        )
        full = db.get_session(row["id"]) or {}
        state.history = [{"role": m["role"], "content": m["content"]} for m in full.get("messages", [])]
        try:
            state.last_candidate_ts = _parse_ts(row["last_ts"])
        except ValueError:
            pass
        registry[state.id] = state
    if registry:
        log.info("restored %d active sessions from storage", len(registry))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, monitor
    db.init_db()
    llm = build_llm()
    agent = ScreeningAgent(llm)
    _load_active_sessions()
    monitor = ReengagementMonitor(agent, registry)
    await monitor.start()
    log.info("Sazon screening agent up (llm=%s)", llm.name)
    yield
    await monitor.stop()


app = FastAPI(title="Grupo Sazon Screening Agent", lifespan=lifespan)


class CreateSessionRequest(BaseModel):
    source: str = "web"
    language: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _get_state(session_id: str) -> SessionState:
    state = registry.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")
    return state


def _persist_turn(state: SessionState, user_text: str, reply: str, decision: TurnDecision) -> None:
    clean = redact_pii(screen_input(user_text).sanitized)
    db.save_session(state)
    db.append_message(state.id, "user", clean, decision.stage_before.value)
    db.append_message(state.id, "assistant", reply, decision.stage_after.value)
    db.append_events(state.id, decision.events, decision.stage_after.value)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": settings.llm_model,
        "llm_available": bool(agent and agent.llm.available()),
        "active_sessions": sum(1 for s in registry.values() if s.status == "active"),
    }


@app.post("/api/sessions")
def create_session(req: CreateSessionRequest) -> dict:
    assert agent is not None
    state = agent.new_session(source=req.source, language=req.language)
    reply = agent.opening(state)
    registry[state.id] = state
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, Stage.INTRO.value)
    return {"session_id": state.id, "reply": reply, "stage": state.stage.value, "language": state.language.value}


@app.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    reply, decision = agent.process(state, req.text)
    _persist_turn(state, req.text, reply, decision)
    return {
        "reply": reply,
        "stage": state.stage.value,
        "status": state.status,
        "language": state.language.value,
        "advanced": decision.advanced,
        "validation_error": decision.validation_error,
        "guardrail": decision.guardrail,
        "rag_used": decision.rag_used,
        "summary": state.summary,
    }


@app.get("/api/sessions/{session_id}/pending")
def pending(session_id: str) -> dict:
    _get_state(session_id)
    return {"messages": db.pending_messages(session_id)}


@app.post("/api/sessions/{session_id}/end")
def end_session(session_id: str) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    if state.stage in (Stage.QUALIFIED, Stage.DISQUALIFIED, Stage.ABANDONED):
        return {"reply": "", "status": state.status}
    decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
    stage = Stage.QUALIFIED if state.data.is_qualified() else Stage.ABANDONED
    reply = agent._close(state, decision, stage, None, None)
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, state.stage.value)
    db.append_events(state.id, decision.events, state.stage.value)
    return {"reply": reply, "status": state.status, "summary": state.summary}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.get("/api/sessions")
def list_sessions(
    status: str | None = Query(None),
    qualified: bool | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    rows = db.list_sessions(status=status, qualified=qualified, q=q, limit=limit)
    for row in rows:
        row.pop("data_json", None)
    return {"sessions": rows}


@app.get("/api/analytics")
def analytics() -> dict:
    return metrics.compute()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND / "dashboard.html")


app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
