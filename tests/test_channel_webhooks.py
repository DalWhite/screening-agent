"""HTTP-level tests for the Twilio webhook endpoints.

The full FastAPI app is booted (with the MockLLM and a throwaway database) and
webhooks are posted exactly the way Twilio posts them: form-encoded bodies plus
an X-Twilio-Signature header. Twilio itself is never contacted.
"""

import re

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from backend.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "webhooks.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "twilio_validate_signatures", False)
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app) as test_client:
        yield test_client


def test_whatsapp_webhook_returns_twiml(client):
    resp = client.post(
        "/api/channels/whatsapp/inbound",
        data={"From": "whatsapp:+34600111222", "Body": "hola", "MessageSid": "SM1"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Message>" in resp.text and "Grupo Sazón" in resp.text

    sessions = client.get("/api/sessions").json()["sessions"]
    ours = [s for s in sessions if s["source"] == "whatsapp"]
    assert ours and ours[0]["contact"] == "+34600111222"


def test_whatsapp_webhook_multi_turn(client):
    client.post("/api/channels/whatsapp/inbound", data={"From": "whatsapp:+34600111222", "Body": "hola"})
    resp = client.post(
        "/api/channels/whatsapp/inbound",
        data={"From": "whatsapp:+34600111222", "Body": "Hola, quiero aplicar para repartidor"},
    )
    assert "nombre" in resp.text.lower()  # advanced to the NAME question


def test_voice_webhook_call_flow(client):
    resp = client.post("/api/channels/voice/inbound", data={"From": "+34600111333", "CallSid": "CA77"})
    assert resp.status_code == 200
    assert "<Gather" in resp.text
    session_id = re.search(r"session_id=([0-9a-f-]+)", resp.text).group(1)

    resp = client.post(
        f"/api/channels/voice/collect?session_id={session_id}&retries=0",
        data={"CallSid": "CA77", "SpeechResult": "Hola, quiero aplicar para repartidor", "Confidence": "0.91"},
    )
    assert resp.status_code == 200
    assert "<Gather" in resp.text and "nombre" in resp.text.lower()

    resp = client.post("/api/channels/voice/status", data={"CallSid": "CA77", "CallStatus": "completed"})
    assert resp.status_code == 204

    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["source"] == "voice"
    assert any(e["type"] == "voice_call_ended" for e in session["events"])


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test-auth-token")
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)

    resp = client.post(
        "/api/channels/whatsapp/inbound",
        data={"From": "whatsapp:+34600111222", "Body": "hola"},
        headers={"X-Twilio-Signature": "forged"},
    )
    assert resp.status_code == 403


def test_webhook_accepts_valid_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test-auth-token")
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)

    url = "http://testserver/api/channels/whatsapp/inbound"
    params = {"From": "whatsapp:+34600111222", "Body": "hola"}
    signature = RequestValidator("test-auth-token").compute_signature(url, params)
    resp = client.post(
        "/api/channels/whatsapp/inbound",
        data=params,
        headers={"X-Twilio-Signature": signature},
    )
    assert resp.status_code == 200
    assert "<Message>" in resp.text


def test_signature_uses_public_base_url_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test-auth-token")
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)
    monkeypatch.setattr(settings, "public_base_url", "https://screening.example.com")

    # Twilio signs the public URL, not whatever host the proxy forwarded to us
    url = "https://screening.example.com/api/channels/voice/inbound"
    params = {"From": "+34600111333", "CallSid": "CA88"}
    signature = RequestValidator("test-auth-token").compute_signature(url, params)
    resp = client.post("/api/channels/voice/inbound", data=params, headers={"X-Twilio-Signature": signature})
    assert resp.status_code == 200
    assert "<Gather" in resp.text


# ---------- bare-host webhook URL (common console misconfiguration) ----------

def test_root_post_dispatches_whatsapp_payload(client):
    resp = client.post("/", data={"From": "whatsapp:+34600111222", "Body": "hola", "MessageSid": "SM9"})
    assert resp.status_code == 200
    assert "<Message>" in resp.text


def test_root_post_dispatches_voice_payload(client):
    resp = client.post("/", data={"From": "+34600111333", "CallSid": "CA90", "CallStatus": "ringing"})
    assert resp.status_code == 200 and "<Gather" in resp.text
    resp = client.post("/", data={"CallSid": "CA90", "CallStatus": "completed"})
    assert resp.status_code == 204


def test_root_post_rejects_non_twilio_payload(client):
    assert client.post("/", data={"foo": "bar"}).status_code == 404


def test_root_post_still_validates_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test-auth-token")
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)
    monkeypatch.setattr(settings, "public_base_url", "https://m3c7wktg-8000.uks1.devtunnels.ms")
    params = {"From": "whatsapp:+34600111222", "Body": "hola", "MessageSid": "SM9"}
    url = "https://m3c7wktg-8000.uks1.devtunnels.ms/"
    good = RequestValidator("test-auth-token").compute_signature(url, params)
    assert client.post("/", data=params, headers={"X-Twilio-Signature": good}).status_code == 200
    assert client.post("/", data=params, headers={"X-Twilio-Signature": "forged"}).status_code == 403
