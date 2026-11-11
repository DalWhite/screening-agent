"""The simulator's Meta Cloud API, media, speech-to-text and delivery receipts.

Run against a tiny capture app (not the screening app): failures here mean the
simulator does not behave like the real platforms.
"""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from simulators.speech import make_voice_note, read_voice_note
from simulators.server import create_app
from simulators.core import ChannelSimulator
from simulators.twilio import DEFAULT_ACCOUNT_SID, DEFAULT_AUTH_TOKEN

NUMBER = "+34600111222"
SIM = "http://simulator.test"


def capture_app(received: list) -> FastAPI:
    app = FastAPI()

    @app.get("/api/channels/whatsapp/meta")
    def verify(request: Request):
        q = request.query_params
        if q.get("hub.verify_token") != "simulated-verify-token":
            return PlainTextResponse("no", status_code=403)
        return PlainTextResponse(q.get("hub.challenge"))

    @app.post("/api/channels/whatsapp/meta")
    async def meta(request: Request):
        raw = await request.body()
        received.append({"kind": "meta", "json": json.loads(raw), "raw": raw,
                         "signature": request.headers.get("X-Hub-Signature-256")})
        return {"ok": True}

    @app.post("/twilio-status")
    async def twilio_status(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        ok = RequestValidator(DEFAULT_AUTH_TOKEN).validate(str(request.url), form,
                                                           request.headers.get("X-Twilio-Signature", ""))
        received.append({"kind": "twilio_status", "form": form, "signed": ok})
        return Response(status_code=204)

    return app


@pytest.fixture
def received():
    return []


@pytest.fixture
def sim(received):
    client = TestClient(capture_app(received))
    simulator = ChannelSimulator("http://testserver", transport=client, whatsapp_provider="meta", graph_url=SIM)
    yield simulator
    simulator.close()
    client.close()


@pytest.fixture
def http(sim):
    with TestClient(create_app(sim), base_url=SIM) as client:
        yield client


BEARER = {"Authorization": "Bearer EAAG-simulated-access-token"}
MESSAGES = "/v21.0/106540352242922/messages"


def graph_send(http, payload, headers=BEARER):
    return http.post(MESSAGES, json={"messaging_product": "whatsapp", **payload}, headers=headers)


# ---------- audio ----------

def test_simulated_voice_notes_are_real_wav_with_their_transcript():
    import io
    import wave

    data = make_voice_note("vivo en Getafe", "es")
    assert wave.open(io.BytesIO(data)).getnframes() > 0
    assert read_voice_note(data)["transcript"] == "vivo en Getafe"
    assert read_voice_note(b"OggS real audio") is None


# ---------- Meta webhooks ----------

def test_inbound_messages_are_signed_meta_payloads(sim, received):
    sim.phone(NUMBER, "Laura").whatsapp("hola")
    hook = received[0]
    expected = "sha256=" + hmac.new(b"simulated-app-secret", hook["raw"], hashlib.sha256).hexdigest()
    assert hook["signature"] == expected
    value = hook["json"]["entry"][0]["changes"][0]["value"]
    assert value["metadata"]["phone_number_id"] == "106540352242922"
    assert value["contacts"][0] == {"profile": {"name": "Laura"}, "wa_id": "34600111222"}
    message = value["messages"][0]
    assert message["type"] == "text" and message["text"]["body"] == "hola" and message["id"].startswith("wamid.")


def test_voice_notes_and_photos_reference_media_ids(sim, received):
    phone = sim.phone(NUMBER)
    phone.voice_note("sí")
    phone.photo("mi carnet")
    audio = received[0]["json"]["entry"][0]["changes"][0]["value"]["messages"][0]
    image = received[1]["json"]["entry"][0]["changes"][0]["value"]["messages"][0]
    assert audio["type"] == "audio" and audio["audio"]["voice"] is True and audio["audio"]["mime_type"].startswith("audio/ogg")
    assert image["type"] == "image" and image["image"]["caption"] == "mi carnet"
    assert audio["audio"]["id"] in sim.media


def test_subscription_handshake(sim):
    assert sim.meta.verify_webhook()[0] == 200
    assert sim.meta.verify_webhook(token="wrong")[0] == 403
    assert "verification failed" in sim.log[-1].error


# ---------- Graph API ----------

def test_text_is_delivered_and_statuses_follow(sim, http, received):
    phone = sim.phone(NUMBER)
    phone.whatsapp("hola")
    resp = graph_send(http, {"to": "34600111222", "type": "text", "text": {"body": "¿Cómo te llamas?"}})
    assert resp.status_code == 200
    wamid = resp.json()["messages"][0]["id"]
    assert phone.texts()[-1] == "¿Cómo te llamas?"
    sim.flush()
    statuses = [r["json"]["entry"][0]["changes"][0]["value"]["statuses"][0] for r in received[1:]]
    assert [(s["id"], s["status"]) for s in statuses] == [(wamid, "sent"), (wamid, "delivered")]
    phone.whatsapp("Laura")  # replying = reading (receipts are async: no ordering guarantee, like Meta)
    sim.flush()
    values = [r["json"]["entry"][0]["changes"][0]["value"] for r in received]
    assert (wamid, "read") in [(st["id"], st["status"]) for v in values for st in v.get("statuses", [])]


def test_outside_the_window_text_is_accepted_then_fails_with_131047(sim, http, received):
    resp = graph_send(http, {"to": "34600111222", "type": "text", "text": {"body": "¿Sigues ahí?"}})
    assert resp.status_code == 200  # accepted...
    sim.flush()
    failed = received[-1]["json"]["entry"][0]["changes"][0]["value"]["statuses"][0]
    assert failed["status"] == "failed" and failed["errors"][0]["code"] == 131047  # ... failed later
    assert sim.phone(NUMBER).received() == []


def test_templates_are_delivered_outside_the_window(sim, http):
    sim.meta.templates["reengagement"] = "Hola {{1}}, soy {{2}}."
    resp = graph_send(http, {"to": "34600111222", "type": "template", "template": {
        "name": "reengagement", "language": {"code": "es"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "Laura"}, {"type": "text", "text": "Marisol"}]}]}})
    assert resp.json()["messages"][0]["message_status"] == "accepted"
    assert sim.phone(NUMBER).texts() == ["Hola Laura, soy Marisol."]


@pytest.mark.parametrize("setup, payload, headers, status, code", [
    (None, {"to": "34600111222", "type": "text", "text": {"body": "x"}}, {"Authorization": "Bearer nope"}, 401, 190),
    (None, {"to": "34600111222", "type": "template", "template": {"name": "ghost"}}, BEARER, 404, 132001),
    (None, {"to": "34600111222", "type": "image"}, BEARER, 400, 100),
    ("allow", {"to": "34600111222", "type": "text", "text": {"body": "x"}}, BEARER, 400, 131030),
    ("down", {"to": "34600111222", "type": "text", "text": {"body": "x"}}, BEARER, 503, 2),
])
def test_graph_errors_use_metas_envelope(sim, http, setup, payload, headers, status, code):
    if setup == "allow":
        sim.faults.recipient_allow_list = ["+34999999999"]
    if setup == "down":
        sim.faults.rest_down = True
    resp = graph_send(http, payload, headers)
    assert resp.status_code == status and resp.json()["error"]["code"] == code


def test_unknown_phone_number_id(http):
    resp = http.post("/v21.0/999/messages", json={"messaging_product": "whatsapp", "to": "1", "type": "text"},
                     headers=BEARER)
    assert resp.status_code == 400 and resp.json()["error"]["type"] == "GraphMethodException"


def test_media_is_two_steps_with_the_bearer_token(sim, http):
    phone = sim.phone(NUMBER)
    phone.voice_note("sí, tengo carnet")
    media_id = phone.thread[-1].media_id
    info = http.get(f"/v21.0/{media_id}", headers=BEARER).json()
    assert info["url"].startswith(SIM) and info["mime_type"].startswith("audio/ogg")
    assert http.get(info["url"]).status_code == 401  # the URL alone is not enough
    audio = http.get(info["url"], headers=BEARER)
    assert read_voice_note(audio.content)["transcript"] == "sí, tengo carnet"


def test_read_receipt_from_the_business(sim, http):
    phone = sim.phone(NUMBER)
    phone.whatsapp("hola")
    graph_send(http, {"status": "read", "message_id": phone.thread[-1].sid})
    assert phone.thread[-1].read


# ---------- speech-to-text ----------

def test_speech_to_text_endpoint(sim, http):
    note = make_voice_note("sí, tengo carnet", "es")
    auth = {"Authorization": "Bearer sim-stt-key"}
    files = {"file": ("note.ogg", note, "audio/ogg")}
    body = http.post("/v1/audio/transcriptions", files=files, data={"model": "whisper-1"}, headers=auth).json()
    assert body["text"] == "sí, tengo carnet" and body["language"] == "spanish" and body["duration"] > 0
    assert http.post("/v1/audio/transcriptions", files=files, data={"model": "whisper-1"}).status_code == 401
    assert http.post("/v1/audio/transcriptions", files={"file": ("x.ogg", b"OggS", "audio/ogg")},
                     headers=auth).json() == {"text": ""}
    sim.faults.stt_down = True
    assert http.post("/v1/audio/transcriptions", files=files, headers=auth).status_code == 503


# ---------- Twilio side: media, status callbacks ----------

@pytest.fixture
def twilio_sim(received):
    client = TestClient(capture_app(received))
    simulator = ChannelSimulator("http://testserver", transport=client, graph_url=SIM)
    yield simulator
    simulator.close()
    client.close()


def test_twilio_media_needs_account_credentials(twilio_sim):
    import base64

    phone = twilio_sim.phone(NUMBER)
    media = twilio_sim.store_media(make_voice_note("hola"), "audio/ogg")
    path = f"/2010-04-01/Accounts/{DEFAULT_ACCOUNT_SID}/Messages/MM1/Media/ME{media.id}"
    with TestClient(create_app(twilio_sim), base_url=SIM) as http:
        assert http.get(path).status_code == 401
        auth = "Basic " + base64.b64encode(f"{DEFAULT_ACCOUNT_SID}:{DEFAULT_AUTH_TOKEN}".encode()).decode()
        resp = http.get(path, headers={"Authorization": auth})
        assert resp.status_code == 200 and read_voice_note(resp.content)["transcript"] == "hola"
    assert phone.number == NUMBER


@pytest.mark.parametrize("window_errors, expected", [("sync", None), ("async", ["sent", "undelivered"])])
def test_twilio_status_callbacks(twilio_sim, received, window_errors, expected):
    twilio_sim.faults.window_errors = window_errors
    rest = twilio_sim.twilio.rest_client()
    phone = twilio_sim.phone(NUMBER)
    if expected is None:  # inside the window: sent -> delivered
        phone.whatsapp("hola")
        received.clear()
        expected = ["sent", "delivered"]
    rest.messages.create(to=f"whatsapp:{NUMBER}", from_="whatsapp:+14155238886", body="¿Sigues ahí?",
                         status_callback="http://testserver/twilio-status")
    twilio_sim.flush()
    callbacks = [r for r in received if r["kind"] == "twilio_status"]
    assert [c["form"]["MessageStatus"] for c in callbacks] == expected and all(c["signed"] for c in callbacks)
    if expected[-1] == "undelivered":
        assert callbacks[-1]["form"]["ErrorCode"] == "63016"
