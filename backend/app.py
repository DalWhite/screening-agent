from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import metrics
from .api.sessions import router as sessions_router
from .backoffice.auth import require_recruiter
from .backoffice.router import router as backoffice_router
from .backoffice.profiles_admin import ProfileAdmin
from .backoffice.service import BackofficeService
from .channels.hub import ChannelHub
from .channels.voice import VoiceChannel
from .channels.messaging import MessagingChannel
from .config import settings
from .llm.client import build_llm
from .reengagement import ReengagementMonitor
from .screening.agent import ScreeningAgent, SessionState
from .screening.profile import registry as profile_registry
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
hub: ChannelHub | None = None  # channels and their providers (backend/channels/hub.py)
# Conveniences (tests, scripts): the channels the hub runs, or None when turned off.
whatsapp_channel: MessagingChannel | None = None
voice_channel: VoiceChannel | None = None


def _nudge_notifier(state: SessionState, text: str) -> Optional[str]:
    """Re-engagement nudges: pushed on a channel the candidate can be written to (a template may stand in)."""
    return hub.nudge(state, text) if hub is not None else None  # None: stored, picked up via /pending or later


def _message_notifier(state: SessionState, text: str) -> Optional[str]:
    """Recruiter messages and check decisions: delivered only if the candidate gets these words."""
    return hub.notify(state, text) if hub is not None else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, monitor, hub, whatsapp_channel, voice_channel
    db.init_db()
    llm = build_llm()
    profiles = profile_registry()
    profile_store = ProfileStore()
    profiles.attach_store(profile_store)  # seeds imported once; edits and revisions live in the database
    app.state.profiles_admin = ProfileAdmin(profiles, profile_store)
    agent = ScreeningAgent(llm, profiles)
    store.load_active()
    hub = ChannelHub(agent, store)
    whatsapp_channel, voice_channel = hub.messaging.get("whatsapp"), hub.voice
    mounted = [route for router in hub.routers() for route in router.routes]
    app.router.routes[:0] = mounted  # the providers' webhooks (before the static mount)
    app.state.channels = hub
    monitor = ReengagementMonitor(agent, registry, notifier=_nudge_notifier)
    verification_service = VerificationService(agent, store, notifier=_message_notifier)
    verifications.set_service(verification_service)
    app.state.verifications = verification_service
    app.state.backoffice = BackofficeService(agent, store, notifier=_message_notifier,
                                             verifications=verification_service, channels=hub.describe())
    await monitor.start()
    await verification_service.start()
    log.info("screening agent up (llm=%s, channels=%s, stt=%s, profiles=%s, default=%s)", llm.name,
             ", ".join(f"{kind}:{info['provider']}{'' if info['configured'] else ' (not configured)'}"
                       for kind, info in hub.describe().items()) or "web only",
             hub.transcriber.name, ",".join(p.id for p in profiles.all()), profiles.default().id)
    if not settings.backoffice_password:
        log.warning("BACKOFFICE_PASSWORD is empty: the recruiter portal is open (local development only)")
    yield
    await verification_service.stop()
    verifications.set_service(None)
    await monitor.stop()
    profiles.detach_store()
    app.router.routes[:] = [route for route in app.router.routes if route not in mounted]


app = FastAPI(title="Screening Agent", lifespan=lifespan)
app.include_router(sessions_router)  # the web channel's transport (browser chat, ATS)
app.include_router(backoffice_router)


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


# ---------- channel webhooks ----------
# Mounted at startup by the channel hub: each provider adapter brings its own routes
# (backend/integrations/<vendor>/). See docs/channel_architecture.md.


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
