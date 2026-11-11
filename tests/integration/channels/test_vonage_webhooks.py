"""Vonage Voice through the app: signed webhooks in, NCCO out, and the simulator's Vonage plugin."""


from __future__ import annotations

import json

import pytest

from backend.integrations.vonage.voice import ANSWER, EVENT, INPUT
from backend.config import settings
from simulators.vonage import signed_jwt

SECRET = "test-signature-secret"
CALLER = "+34600111222"


# ---------- the webhooks, through the app ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "vonage.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "voice_provider", "vonage")
    monkeypatch.setattr(settings, "vonage_signature_secret", SECRET)
    monkeypatch.setattr(settings, "vonage_validate_signatures", True)
    import backend.app as appmod

    with TestClient(appmod.app) as test_client:
        yield test_client


def signed(body: dict | None = None) -> dict:
    raw = json.dumps(body).encode() if body is not None else b""
    return {"content": raw, "headers": {"Authorization": f"Bearer {signed_jwt(SECRET, raw)}",
                                        "Content-Type": "application/json"}}


def test_a_call_through_the_vonage_webhooks(client):
    answer = client.get(ANSWER, params={"from": CALLER[1:], "to": "34910000000", "uuid": "call-1"},
                        headers={"Authorization": f"Bearer {signed_jwt(SECRET)}"})
    assert answer.status_code == 200 and "Grupo Sazón" in answer.json()[0]["text"]
    event_url = answer.json()[1]["eventUrl"][0]
    path = event_url.split("testserver", 1)[1]

    both = {"speech": {"results": [{"text": "no", "confidence": "0.40"}, {"text": "sí, empecemos", "confidence": "0.91"}],
                       "timeout_reason": "end_on_silence_timeout"}, "uuid": "call-1"}
    reply = client.post(path, **signed(both)).json()
    assert "nombre" in reply[0]["text"].lower()  # the most confident result was used

    silent = client.post(reply[1]["eventUrl"][0].split("testserver", 1)[1],
                         **signed({"speech": {"timeout_reason": "start_timeout", "results": []}, "uuid": "call-1"}))
    assert "sigues" in silent.json()[0]["text"].lower() and "retries=1" in silent.json()[1]["eventUrl"][0]

    assert client.post(EVENT, **signed({"status": "completed", "uuid": "call-1"})).status_code == 204
    session = client.get("/api/sessions", params={"q": CALLER[1:]}).json()["sessions"][0]
    events = [e["type"] for e in client.get(f"/api/sessions/{session['id']}").json()["events"]]
    assert session["source"] == "voice" and "voice_call_ended" in events


def test_unsigned_or_forged_vonage_webhooks_are_rejected(client):
    assert client.get(ANSWER, params={"from": CALLER[1:], "uuid": "x"}).status_code == 401
    forged = {"Authorization": f"Bearer {signed_jwt('not-the-secret', b'{}')}", "Content-Type": "application/json"}
    assert client.post(EVENT, content=b"{}", headers=forged).status_code == 401
    assert client.get("/api/sessions").json()["sessions"] == []


# ---------- the simulator plugin ----------

def test_the_simulator_signs_like_vonage_and_executes_ncco(channel_stack):
    stack = channel_stack(voice="vonage")
    call = stack.sim.phone(CALLER).call()
    assert call.provider == "vonage" and call.waiting and call.gather_language == "es-ES"
    answer = next(e for e in stack.sim.log if e.url.endswith(ANSWER) or ANSWER in e.url)
    assert answer.method == "GET" and answer.params["from"] == CALLER[1:]  # Vonage numbers have no "+"
    call.say("sí")
    posted = [e for e in stack.sim.log if INPUT in e.url][-1].params
    assert posted["speech"]["results"][0]["text"] == "Sí." and posted["uuid"] == call.sid
    call.hangup()
    stack.sim.flush()
    ended = [e for e in stack.sim.log if EVENT in e.url][-1]
    assert ended.params["status"] == "completed" and ended.status == 204


def test_a_forged_call_never_reaches_the_screening(channel_stack):
    stack = channel_stack(voice="vonage")
    stack.sim.vonage.signature_secret = "someone-else"
    call = stack.sim.phone(CALLER).call()
    assert call.end_reason == "application-error" and stack.sessions_for(CALLER) == []
