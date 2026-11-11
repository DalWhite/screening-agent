"""Vonage Voice: the adapter (JWT-signed webhooks, NCCO) and its simulator plugin.

The screening stories themselves run on Vonage through the contract tests
(``voice_provider`` in tests/integration/channels/test_stories.py / test_conversation_registry.py).
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from backend.channels.hub import ChannelHub
from backend.integrations.vonage.voice import INPUT, verify_signed_webhook
from backend.channels.voice import VoiceAction
from backend.config import settings
from simulators.vonage import signed_jwt

SECRET = "test-signature-secret"


# ---------- signed webhooks ----------

def test_a_signed_webhook_verifies():
    body = b'{"status": "completed"}'
    assert verify_signed_webhook(f"Bearer {signed_jwt(SECRET, body)}", SECRET, body)
    assert verify_signed_webhook(f"Bearer {signed_jwt(SECRET)}", SECRET, b"")  # GET answer: no body


@pytest.mark.parametrize("case", ["wrong secret", "tampered body", "expired", "no token", "alg none", "garbage"])
def test_bad_signatures_are_rejected(case):
    body = b'{"status": "completed"}'
    token = signed_jwt(SECRET, body)
    if case == "wrong secret":
        token = signed_jwt("another", body)
    elif case == "tampered body":
        body = b'{"status": "answered"}'
    elif case == "expired":
        token = signed_jwt(SECRET, body, issued_at=int(time.time()) - 3600)
    elif case == "alg none":
        header, payload, _ = token.split(".")
        import base64
        token = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode() + f".{payload}."
    elif case == "garbage":
        token = "a.b.c"
    authorization = "" if case == "no token" else f"Bearer {token}"
    assert not verify_signed_webhook(authorization, SECRET, body)


# ---------- NCCO ----------

@pytest.fixture
def vonage(agent, store, monkeypatch):
    monkeypatch.setattr(settings, "vonage_signature_secret", SECRET)
    return ChannelHub(agent, store, selection={"voice": "vonage"}).adapter("voice")


def test_a_question_is_talk_then_speech_input(vonage):
    ncco = vonage.render(VoiceAction("¿Cómo te llamas? 😊", "es-ES", session_id="s-1", retries=1), "https://app.test")
    talk, listen = ncco
    assert talk == {"action": "talk", "text": "¿Cómo te llamas?", "language": "es-ES", "bargeIn": False}
    assert listen["action"] == "input" and listen["type"] == ["speech"]
    assert listen["eventUrl"] == [f"https://app.test{INPUT}?session_id=s-1&retries=1"]
    assert listen["speech"]["language"] == "es-ES"


def test_a_goodbye_is_talk_only_so_the_call_ends(vonage):
    assert vonage.render(VoiceAction("¡Gracias!", "en-US", session_id="s-1", listen=False), "https://x") == [
        {"action": "talk", "text": "¡Gracias!", "language": "en-US", "bargeIn": False}]


def test_payload_hash_binds_the_body():
    raw = b'{"speech": {}}'
    token = signed_jwt(SECRET, raw)
    import base64

    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert payload["payload_hash"] == hashlib.sha256(raw).hexdigest()
