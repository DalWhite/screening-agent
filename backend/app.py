from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import metrics
from .channels import TwilioProvider, VoiceChannel, WhatsAppChannel
from .config import settings
from .llm.client import build_llm
from .reengagement import ReengagementMonitor
from .screening.agent import ScreeningAgent, SessionState
from .screening.guardrails import redact_pii, screen_input
from .screening.schema import Stage, TurnDecision
from .session_store import SessionStore
from .storage import db

log = logging.getLogger("sazon.app")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

agent: ScreeningAgent | None = None
store = SessionStore()
registry = store.registry  # shared in-memory session registry
monitor: ReengagementMonitor | None = None
twilio_provider: TwilioProvider | None = None
whatsapp_channel: WhatsAppChannel | None = None
voice_channel: VoiceChannel | None = None


def _nudge_notifier(state: SessionState, text: str) -> bool:
    """Push re-engagement nudges over the candidate's channel when possible."""
    if whatsapp_channel is not None and state.source == "whatsapp":
        return whatsapp_channel.send_outbound(state, text)
    return False  # web/voice: stored undelivered, picked up via /pending or call-back


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, monitor, twilio_provider, whatsapp_channel, voice_channel
    db.init_db()
    llm = build_llm()
    agent = ScreeningAgent(llm)
    store.load_active()
    twilio_provider = TwilioProvider()
    whatsapp_channel = WhatsAppChannel(agent, store, twilio_provider)
    voice_channel = VoiceChannel(agent, store)
    monitor = ReengagementMonitor(agent, registry, notifier=_nudge_notifier)
    await monitor.start()
    log.info("Sazon screening agent up (llm=%s, twilio=%s)", llm.name,
             "configured" if twilio_provider.configured else "off")
    if twilio_provider.configured:
        log.info("twilio channels: whatsapp_from=%s public_base_url=%s signatures=%s sync_timeout=%.0fs",
                 settings.twilio_whatsapp_from or "<unset: outbound/late replies disabled>",
                 settings.public_base_url or "<unset: using request host>",
                 settings.twilio_validate_signatures, settings.whatsapp_sync_timeout)
        if not settings.twilio_whatsapp_from:
            log.warning("TWILIO_WHATSAPP_FROM is empty: nudges and late replies cannot be sent")
    yield
    await monitor.stop()


app = FastAPI(title="Grupo Sazon Screening Agent", lifespan=lifespan)


class CreateSessionRequest(BaseModel):
    source: str = "web"
    language: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _get_state(session_id: str) -> SessionState:
    state = store.get(session_id)
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


def _resume_if_needed(state: SessionState) -> str | None:
    """Reopen an abandoned session when the candidate comes back in time."""
    assert agent is not None
    if state.status != "abandoned":
        return None
    age = datetime.now(timezone.utc) - state.last_candidate_ts
    if age > timedelta(hours=settings.resume_window_hours):
        raise HTTPException(status_code=410, detail="resume window expired; start a new conversation")
    reply = agent.resume(state)
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, state.stage.value)
    db.append_events(state.id, [{"type": "session_resume", "payload": {"stage": state.stage.value}}], state.stage.value)
    return reply


@app.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    resume_reply = _resume_if_needed(state)
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
        "resumed": resume_reply is not None,
        "resume_reply": resume_reply,
        "summary": state.summary,
    }


@app.get("/api/sessions/{session_id}/pending")
def pending(session_id: str) -> dict:
    _get_state(session_id)
    return {"messages": db.pending_messages(session_id)}


@app.post("/api/sessions/{session_id}/resume")
def resume_session(session_id: str) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    if state.stage in (Stage.QUALIFIED, Stage.DISQUALIFIED):
        raise HTTPException(status_code=409, detail="final decisions cannot be resumed")
    if state.status == "active":
        reply = agent.resume(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, state.stage.value)
        return {"reply": reply, "stage": state.stage.value, "status": state.status, "language": state.language.value}
    resume_reply = _resume_if_needed(state)
    return {
        "reply": resume_reply or agent.resume(state),
        "stage": state.stage.value,
        "status": state.status,
        "language": state.language.value,
    }


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


# ---------- Twilio channel webhooks (WhatsApp + Voice) ----------


async def _twilio_form(request: Request) -> dict[str, str]:
    """Read Twilio's form-encoded webhook body and enforce signature validation."""
    assert twilio_provider is not None
    form = {k: str(v) for k, v in (await request.form()).items()}
    if settings.public_base_url:
        query = f"?{request.url.query}" if request.url.query else ""
        url = f"{settings.public_base_url}{request.url.path}{query}"
    else:
        url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not twilio_provider.validate_signature(url, form, signature):
        log.warning("rejected webhook with invalid Twilio signature: %s", request.url.path)
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@app.post("/api/channels/whatsapp/inbound")
async def whatsapp_inbound(request: Request) -> Response:
    """Twilio WhatsApp webhook: one inbound candidate message per request."""
    assert whatsapp_channel is not None
    form = await _twilio_form(request)
    xml = await whatsapp_channel.handle_inbound_async(form.get("From", ""), form.get("Body", ""))
    return _twiml(xml)


@app.post("/api/channels/voice/inbound")
async def voice_inbound(request: Request) -> Response:
    """Twilio Voice webhook: incoming call — greet and start gathering speech."""
    assert voice_channel is not None
    form = await _twilio_form(request)
    xml = voice_channel.handle_call_start(form.get("From", ""), form.get("CallSid", ""))
    return _twiml(xml)


@app.post("/api/channels/voice/collect")
async def voice_collect(request: Request, session_id: str, retries: int = 0) -> Response:
    """Twilio Gather action: the candidate's transcribed speech for one turn."""
    assert voice_channel is not None
    form = await _twilio_form(request)
    xml = voice_channel.handle_speech(session_id, form.get("SpeechResult", ""), retries=retries)
    return _twiml(xml)


@app.post("/api/channels/voice/status")
async def voice_status(request: Request) -> Response:
    """Twilio status callback: record how the call ended."""
    assert voice_channel is not None
    form = await _twilio_form(request)
    voice_channel.handle_call_ended(form.get("CallSid", ""), form.get("CallStatus", ""))
    return Response(status_code=204)


@app.post("/")
async def twilio_root_dispatch(request: Request) -> Response:
    """Safety net for a webhook configured as the bare host URL in the Twilio console.

    Twilio posts form-encoded bodies; we recognise them by shape and route to
    the right channel. Explicit `/api/channels/...` URLs remain the recommended
    setup, so this logs a warning pointing at them.
    """
    assert whatsapp_channel is not None and voice_channel is not None
    form = await _twilio_form(request)
    if "MessageSid" in form or "Body" in form:
        log.warning("Twilio messaging webhook hit '/': configure %s/api/channels/whatsapp/inbound instead",
                    settings.public_base_url or "<PUBLIC_BASE_URL>")
        return _twiml(await whatsapp_channel.handle_inbound_async(form.get("From", ""), form.get("Body", "")))
    if "CallSid" in form:
        if form.get("CallStatus") in {"completed", "busy", "failed", "no-answer", "canceled"}:
            voice_channel.handle_call_ended(form["CallSid"], form["CallStatus"])
            return Response(status_code=204)
        log.warning("Twilio voice webhook hit '/': configure %s/api/channels/voice/inbound instead",
                    settings.public_base_url or "<PUBLIC_BASE_URL>")
        return _twiml(voice_channel.handle_call_start(form.get("From", ""), form.get("CallSid", "")))
    raise HTTPException(
        status_code=404,
        detail="Not a Twilio webhook. Channel endpoints: /api/channels/whatsapp/inbound, "
               "/api/channels/voice/inbound, /api/channels/voice/status",
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND / "dashboard.html")


app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
