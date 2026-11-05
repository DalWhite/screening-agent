from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import metrics
from .backoffice.auth import require_recruiter
from .backoffice.router import router as backoffice_router
from .backoffice.profiles_admin import ProfileAdmin
from .backoffice.service import BackofficeService
from .channels import TwilioProvider, VoiceChannel, WhatsAppChannel
from .channels.base import persist_turn
from .config import settings
from .llm.client import build_llm
from .reengagement import ReengagementMonitor
from .screening.agent import ScreeningAgent, SessionState
from .screening.profile import registry as profile_registry
from .screening.schema import FINAL_STAGES, Stage, TERMINAL_STAGES
from .session_store import SessionStore
from .storage import db
from .storage.profile_store import ProfileStore
from . import verifications
from .verifications.providers import signature_ok
from .verifications.service import VerificationError, VerificationService

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
    profiles = profile_registry()
    profile_store = ProfileStore()
    profiles.attach_store(profile_store)  # seeds imported once; edits and revisions live in the database
    app.state.profiles_admin = ProfileAdmin(profiles, profile_store)
    agent = ScreeningAgent(llm, profiles)
    store.load_active()
    twilio_provider = TwilioProvider()
    whatsapp_channel = WhatsAppChannel(agent, store, twilio_provider, profile_id=settings.whatsapp_profile or None)
    voice_channel = VoiceChannel(agent, store, profile_id=settings.voice_profile or None)
    monitor = ReengagementMonitor(agent, registry, notifier=_nudge_notifier)
    verification_service = VerificationService(agent, store, notifier=_nudge_notifier)
    verifications.set_service(verification_service)
    app.state.verifications = verification_service
    app.state.backoffice = BackofficeService(agent, store, notifier=_nudge_notifier,
                                             verifications=verification_service)
    await monitor.start()
    await verification_service.start()
    log.info("screening agent up (llm=%s, twilio=%s, profiles=%s, default=%s)", llm.name,
             "configured" if twilio_provider.configured else "off",
             ",".join(p.id for p in profiles.all()), profiles.default().id)
    if not settings.backoffice_password:
        log.warning("BACKOFFICE_PASSWORD is empty: the recruiter portal is open (local development only)")
    if twilio_provider.configured:
        log.info("twilio channels: whatsapp_from=%s public_base_url=%s signatures=%s sync_timeout=%.0fs",
                 settings.twilio_whatsapp_from or "<unset: outbound/late replies disabled>",
                 settings.public_base_url or "<unset: using request host>",
                 settings.twilio_validate_signatures, settings.whatsapp_sync_timeout)
        if not settings.twilio_whatsapp_from:
            log.warning("TWILIO_WHATSAPP_FROM is empty: nudges and late replies cannot be sent")
    yield
    await verification_service.stop()
    verifications.set_service(None)
    await monitor.stop()
    profiles.detach_store()


app = FastAPI(title="Screening Agent", lifespan=lifespan)
app.include_router(backoffice_router)


class CreateSessionRequest(BaseModel):
    source: str = Field(default="web", pattern=r"^[a-z_]{1,20}$")
    language: str | None = None
    profile_id: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _get_state(session_id: str) -> SessionState:
    state = store.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")
    return state


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": settings.llm_model,
        "llm_available": bool(agent and agent.llm.available()),
        "active_sessions": sum(1 for s in registry.values() if s.status == "active"),
    }


@app.get("/api/profiles")
def list_profiles() -> dict:
    """Active recruiting processes (job picker for the candidate chat)."""
    profiles = profile_registry()
    profiles.refresh_if_changed()
    return {
        "default": profiles.default().id,
        "profiles": [
            {
                **{k: v for k, v in p.public().items() if k in ("id", "company", "role", "summary", "sector",
                                                               "agent_name", "languages", "default_language")},
                "questions": [{"id": q.id, "label": q.label} for q in p.questions],
            }
            for p in profiles.all(include_inactive=False)
        ],
    }


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: str, _: str = Depends(require_recruiter)) -> dict:
    try:
        return profile_registry().get(profile_id).public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc


@app.post("/api/sessions")
def create_session(req: CreateSessionRequest) -> dict:
    assert agent is not None
    profile_registry().refresh_if_changed()
    profile_id = req.profile_id or None
    if profile_id and (profile_id not in profile_registry() or not profile_registry().get(profile_id).active):
        raise HTTPException(status_code=404, detail="unknown or inactive profile")
    state = agent.new_session(source=req.source, language=req.language, profile_id=profile_id)
    reply = agent.opening(state)
    registry[state.id] = state
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, Stage.INTRO)
    return {"session_id": state.id, "reply": reply, "stage": state.stage, "language": state.language.value,
            "profile_id": state.profile_id}


def _resume_if_needed(state: SessionState) -> str | None:
    """Reopen an abandoned session when the candidate comes back in time."""
    assert agent is not None
    if state.status != "abandoned" or state.bot_paused:
        return None
    age = datetime.now(timezone.utc) - state.last_candidate_ts
    if age > timedelta(hours=settings.resume_window_hours):
        raise HTTPException(status_code=410, detail="resume window expired; start a new conversation")
    reply = agent.resume(state)
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, state.stage)
    db.append_events(state.id, [{"type": "session_resume", "payload": {"stage": state.stage}}], state.stage)
    return reply


@app.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    with store.lock(state.id):
        resume_reply = _resume_if_needed(state)
        reply, decision = agent.process(state, req.text)
        persist_turn(state, req.text, reply, decision)
    return {
        "reply": reply,
        "paused": decision.paused,
        "bot_paused": state.bot_paused,
        "stage": state.stage,
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
    """Queued outbound messages (nudges, recruiter messages) plus the live session status."""
    state = _get_state(session_id)
    return {
        "messages": db.pending_messages(session_id),
        "session": {"status": state.status, "stage": state.stage, "bot_paused": state.bot_paused,
                    "language": state.language.value},
    }


@app.post("/api/sessions/{session_id}/resume")
def resume_session(session_id: str) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    if state.stage in FINAL_STAGES or state.stage == Stage.VERIFYING:
        raise HTTPException(status_code=409, detail="final decisions cannot be resumed")
    if state.status == "active":
        reply = agent.resume(state)
        db.save_session(state)
        db.append_message(state.id, "assistant", reply, state.stage)
        return {"reply": reply, "stage": state.stage, "status": state.status, "language": state.language.value}
    resume_reply = _resume_if_needed(state)
    return {
        "reply": resume_reply or agent.resume(state),
        "stage": state.stage,
        "status": state.status,
        "language": state.language.value,
    }


@app.post("/api/sessions/{session_id}/end")
def end_session(session_id: str) -> dict:
    assert agent is not None
    state = _get_state(session_id)
    if state.stage in TERMINAL_STAGES:
        return {"reply": "", "status": state.status}
    reply, decision = agent.end(state)
    db.save_session(state)
    db.append_message(state.id, "assistant", reply, state.stage)
    db.append_events(state.id, decision.events, state.stage)
    return {"reply": reply, "status": state.status, "summary": state.summary}


# Recruiter-only columns never leave through the candidate-facing session endpoint.
_RECRUITER_ONLY = ("assignee", "tags", "interview_at", "pipeline_stage", "pipeline_manual", "score", "state",
                   "attempts", "abuse_count")


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
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


@app.get("/api/sessions")
def list_sessions(
    status: str | None = Query(None),
    qualified: bool | None = Query(None),
    q: str | None = Query(None),
    profile_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: str = Depends(require_recruiter),
) -> dict:
    rows = db.list_sessions(status=status, qualified=qualified, q=q, limit=limit, profile_id=profile_id)
    return {"sessions": rows}


@app.get("/api/analytics")
def analytics(profile_id: str | None = Query(None), _: str = Depends(require_recruiter)) -> dict:
    try:
        return metrics.compute(profile_id=profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown profile") from exc


# ---------- external checks: provider callbacks ----------


class VerificationCallback(BaseModel):
    status: str = Field(pattern="^(valid|invalid|error)$")
    detail: str | None = Field(default=None, max_length=300)
    reference: str | None = Field(default=None, max_length=120)


@app.post("/api/verifications/{verification_id}/callback")
async def verification_callback(verification_id: str, request: Request) -> dict:
    """Asynchronous answer of a ``webhook`` provider. The body must be signed with
    ``X-Signature: sha256=<HMAC-SHA256(body, VERIFICATION_WEBHOOK_SECRET)>``."""
    body = await request.body()
    if not settings.verification_webhook_secret:
        raise HTTPException(status_code=403, detail="verification callbacks are disabled: set VERIFICATION_WEBHOOK_SECRET")
    if not signature_ok(body, request.headers.get("X-Signature", ""), settings.verification_webhook_secret):
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        payload = VerificationCallback.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid body") from exc
    service: VerificationService = request.app.state.verifications
    row = service.get(verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="verification not found")
    if row["provider"] != "webhook":
        raise HTTPException(status_code=409, detail="this verification is not answered by a webhook provider")
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: service.complete(verification_id, payload.status, payload.detail, reference=payload.reference))
    except VerificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": verification_id, "status": result["status"]}


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


@app.get("/backoffice")
def backoffice(_: str = Depends(require_recruiter)) -> FileResponse:
    return FileResponse(FRONTEND / "backoffice.html")


@app.get("/dashboard")
def dashboard() -> RedirectResponse:
    """The old single-page dashboard lives on as the backoffice analytics view."""
    return RedirectResponse("/backoffice#/analytics", status_code=307)


app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
