"""HTTP face of the Twilio simulator.

* ``/2010-04-01/...`` — the Twilio REST API the app's SDK calls
  (``TWILIO_API_BASE_URL`` points here).
* ``/sim/api/...`` — control API used by the browser UI and by tests running
  against a live simulator: phones, WhatsApp messages, calls, faults.
* ``/`` — the phone simulator UI.

Endpoints that make the simulator call the app (send a WhatsApp message,
speak on a call) are plain ``def`` handlers, so they run in the thread pool
while the event loop keeps serving the REST calls the app makes back.
"""

from __future__ import annotations

import re
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import ChannelSimulator, Faults, SimulationError
from .twilio import API_VERSION, basic_auth

STATIC = Path(__file__).resolve().parent / "static"


class PhoneIn(BaseModel):
    number: str = Field(min_length=6, max_length=20)
    name: str = Field(default="", max_length=60)


class WhatsAppIn(BaseModel):
    body: str = Field(default="", max_length=1600)
    media: list[str] = Field(default_factory=list, max_length=10)


class SpeechIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=0.92, ge=0, le=1)


class TemplateIn(BaseModel):
    content_sid: str = Field(pattern=r"^(HX\w+|[a-z0-9_]{1,512})$")  # Twilio Content SID or Meta template name
    body: str = Field(min_length=1, max_length=1024)


class VoiceNoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    language: str = Field(default="es", pattern="^(es|en)$")


class PhotoIn(BaseModel):
    caption: str = Field(default="", max_length=1000)


def create_app(sim: ChannelSimulator) -> FastAPI:
    app = FastAPI(title="Twilio simulator", docs_url="/sim/docs", openapi_url="/sim/openapi.json")
    app.state.sim = sim

    # ---------- Twilio REST API ----------

    async def _rest(request: Request, path: str) -> JSONResponse:
        form = {k: str(v) for k, v in (await request.form()).items()} if request.method == "POST" else {}
        params = {**dict(request.query_params), **form}
        status, body = sim.twilio.handle_rest(request.method, f"/{API_VERSION}/{path}", params,
                                       basic_auth(request.headers.get("authorization", "")))
        return JSONResponse(body, status_code=status)

    @app.get(f"/{API_VERSION}/Accounts/{{account}}/Messages/{{message}}/Media/{{media}}", include_in_schema=False)
    def twilio_media(account: str, message: str, media: str, request: Request) -> Response:
        status, result = sim.twilio.media(request.url.path, basic_auth(request.headers.get("authorization", "")))
        if status != 200:
            return JSONResponse(result, status_code=status)
        return Response(result.data, media_type=result.mime)

    @app.api_route(f"/{API_VERSION}/{{path:path}}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def twilio_rest(path: str, request: Request) -> JSONResponse:
        return await _rest(request, path)

    # ---------- speech-to-text (OpenAI-compatible) ----------

    @app.post("/v1/audio/transcriptions", include_in_schema=False)
    async def transcriptions(request: Request) -> JSONResponse:
        form = await request.form()
        upload = form.get("file")
        data = await upload.read() if upload is not None and hasattr(upload, "read") else b""
        status, body = sim.speech.transcribe(data, request.headers.get("authorization", ""))
        if status == 200 and form.get("response_format") == "text":
            return Response(body.get("text", ""), media_type="text/plain")
        return JSONResponse(body, status_code=status)

    # ---------- Meta: media download + UI playback ----------

    @app.get("/meta-media/{media_id}", include_in_schema=False)
    def meta_media(media_id: str, request: Request) -> Response:
        media = sim.media.get(media_id)
        if not sim.meta.authorized(request.headers.get("authorization", "")):
            return JSONResponse({"error": {"message": "Invalid OAuth access token", "code": 190}}, status_code=401)
        if media is None:
            return JSONResponse({"error": {"message": "media not found", "code": 100}}, status_code=404)
        return Response(media.data, media_type=media.mime)

    @app.get("/sim/media/{media_id}", include_in_schema=False)
    def ui_media(media_id: str) -> Response:
        media = sim.media.get(media_id)
        if media is None:
            raise HTTPException(status_code=404)
        return Response(media.data, media_type=media.mime.split(";")[0])

    # ---------- control API ----------

    def _phone(number: str):
        phone = sim.phones.get(number) or sim.phones.get("+" + number.strip().lstrip("+"))
        if phone is None:
            raise HTTPException(status_code=404, detail="unknown phone")
        return phone

    def _call(call_sid: str):
        for phone in sim.phones.values():
            for call in phone.calls:
                if call.sid == call_sid:
                    return call
        raise HTTPException(status_code=404, detail="unknown call")

    @app.get("/sim/api/state")
    def state(version: int = -1, wait: float = 0) -> dict[str, Any]:
        """Full simulator state. With ``wait``, long-polls until it changes past ``version``."""
        if wait > 0 and version == sim.version:
            sim.wait_for_change(version, min(wait, 25))
        return sim.snapshot()

    @app.post("/sim/api/phones")
    def add_phone(body: PhoneIn) -> dict[str, Any]:
        try:
            return sim.phone(body.number, body.name).to_json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/sim/api/phones/{number}")
    def remove_phone(number: str) -> dict[str, Any]:
        phone = _phone(number)
        with sim._lock:
            sim.phones.pop(phone.number, None)
            sim._changed()
        return {"removed": phone.number}

    @app.post("/sim/api/phones/{number}/whatsapp")
    def send_whatsapp(number: str, body: WhatsAppIn) -> dict[str, Any]:
        if not body.body and not body.media:
            raise HTTPException(status_code=422, detail="empty message")
        replies = _phone(number).whatsapp(body.body, media=body.media)
        return {"replies": replies}

    @app.post("/sim/api/phones/{number}/voice-note")
    def send_voice_note(number: str, body: VoiceNoteIn) -> dict[str, Any]:
        return {"replies": _phone(number).voice_note(body.text, body.language)}

    @app.post("/sim/api/phones/{number}/photo")
    def send_photo(number: str, body: PhotoIn) -> dict[str, Any]:
        return {"replies": _phone(number).photo(body.caption)}

    @app.post("/sim/api/phones/{number}/audio")
    async def send_audio(number: str, request: Request) -> dict[str, Any]:
        """Real recorded audio (UI microphone): needs a real speech-to-text service in the app."""
        phone = _phone(number)
        data = await request.body()
        if not data:
            raise HTTPException(status_code=422, detail="empty audio")
        mime = request.headers.get("content-type", "audio/ogg")
        import asyncio

        replies = await asyncio.get_running_loop().run_in_executor(None, phone.audio, data, mime)
        return {"replies": replies}

    @app.post("/sim/api/meta/verify")
    def meta_verify(token: str = "") -> dict[str, Any]:
        """Meta's webhook subscription handshake, as when saving the URL in the app dashboard."""
        status, body = sim.meta.verify_webhook(token or None)
        return {"status": status, "body": body, "ok": status == 200}

    @app.post("/sim/api/phones/{number}/window")
    def close_window(number: str) -> dict[str, Any]:
        phone = _phone(number)
        phone.close_window()
        return {"number": phone.number, "window_open": phone.window_open()}

    @app.post("/sim/api/phones/{number}/calls")
    def start_call(number: str) -> dict[str, Any]:
        phone = _phone(number)
        if phone.active_call is not None:
            raise HTTPException(status_code=409, detail="this phone is already on a call")
        return phone.call().to_json()

    @app.post("/sim/api/calls/{call_sid}/speech")
    def speak(call_sid: str, body: SpeechIn) -> dict[str, Any]:
        call = _call(call_sid)
        try:
            call.say(body.text, confidence=body.confidence)
        except SimulationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return call.to_json()

    @app.post("/sim/api/calls/{call_sid}/silence")
    def silence(call_sid: str) -> dict[str, Any]:
        call = _call(call_sid)
        try:
            call.silence()
        except SimulationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return call.to_json()

    @app.post("/sim/api/calls/{call_sid}/hangup")
    def hangup(call_sid: str) -> dict[str, Any]:
        call = _call(call_sid)
        call.hangup()
        return call.to_json()

    @app.get("/sim/api/faults")
    def get_faults() -> dict[str, Any]:
        return sim.snapshot()["faults"]

    @app.post("/sim/api/faults")
    def set_faults(changes: dict[str, Any]) -> dict[str, Any]:
        known = {f.name: f.type for f in dc_fields(Faults)}
        unknown = set(changes) - set(known)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown faults: {sorted(unknown)}")
        if "window_errors" in changes and changes["window_errors"] not in ("sync", "async"):
            raise HTTPException(status_code=422, detail="window_errors must be 'sync' or 'async'")
        with sim._lock:
            for key, value in changes.items():
                setattr(sim.faults, key, value)
            sim._changed()
        return sim.snapshot()["faults"]

    @app.post("/sim/api/templates")
    def add_template(body: TemplateIn) -> dict[str, Any]:
        with sim._lock:
            target = sim.twilio.templates if body.content_sid.startswith("HX") else sim.meta.templates
            target[body.content_sid] = body.body
            sim._changed()
        return {"templates": {**sim.twilio.templates, **sim.meta.templates}}

    @app.post("/sim/api/reset")
    def reset() -> dict[str, Any]:
        sim.reset()
        return {"ok": True}

    @app.get("/sim/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "app_url": sim.app_url}

    # ---------- UI ----------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/sim/static", StaticFiles(directory=STATIC), name="sim-static")

    # ---------- Meta Graph API (last: catches /vNN.N/...) ----------

    @app.api_route("/{version}/{path:path}", methods=["GET", "POST"], include_in_schema=False)
    async def graph(version: str, path: str, request: Request) -> JSONResponse:
        if not re.fullmatch(r"v\d+\.\d+", version):
            raise HTTPException(status_code=404)
        body = None
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                body = {}
        import asyncio

        status, response = await asyncio.get_running_loop().run_in_executor(
            None, sim.meta.handle_graph, request.method, f"/{version}/{path}", body,
            request.headers.get("authorization", ""))
        return JSONResponse(response, status_code=status)

    return app
