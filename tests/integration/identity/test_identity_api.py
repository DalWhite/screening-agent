"""Identity analysis §2A / §3-E6 / §4 through the real app: HTTP API, webhooks, background jobs.

The app runs in-process (TestClient, MockLLM, throwaway SQLite); WhatsApp and phone go through the
channel simulator (signed webhooks), like ``tests/integration/channels``.

The unit-level scenarios (channels, races, workers) are in ``tests/unit/identity``; the contract
both use is ``tests/support/identity.py``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.storage import db
from tests.support.identity import (DEFERRED_ANALYTICS, DEFERRED_WEB, assert_same_candidate, construction, courier,
                                    event_types, is_opening, sessions_for, total_candidates, user_lines)

LAURA = "+34600111222"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app alone (no channel providers needed): the session API as the web chat and an ATS use it."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "identity.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "backoffice_password", "")
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app) as test_client:
        yield test_client


def session_count() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


# ======================= §4: client retries on the session API =======================

@DEFERRED_WEB
def test_retrying_session_creation_with_an_idempotency_key_opens_one_session(client):
    """An ATS (or the chat on a flaky mobile network) retries a create whose answer it never got."""
    headers = {"Idempotency-Key": "ats-application-4711"}
    body = {"profile_id": "delivery_courier", "source": "ats"}

    first = client.post("/api/sessions", json=body, headers=headers).json()
    retry = client.post("/api/sessions", json=body, headers=headers).json()

    assert retry["session_id"] == first["session_id"]
    assert session_count() == 1


@DEFERRED_WEB
def test_retrying_a_message_with_an_idempotency_key_is_processed_once(client):
    """The answer to "¿tienes carnet?" is sent again after a timeout: one turn, the same reply."""
    sid = client.post("/api/sessions", json={"profile_id": "delivery_courier"}).json()["session_id"]
    for text in courier()[:2]:
        client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    headers = {"Idempotency-Key": f"{sid}-turn-3"}

    first = client.post(f"/api/sessions/{sid}/messages", json={"text": "sí, tengo carnet"}, headers=headers).json()
    retry = client.post(f"/api/sessions/{sid}/messages", json={"text": "sí, tengo carnet"}, headers=headers).json()

    assert first["stage"] == "location"
    assert (retry["stage"], retry["reply"]) == (first["stage"], first["reply"]), "the retry ran a second turn"
    assert user_lines(sid).count("sí, tengo carnet") == 1


# ======================= §2A: a session that knows the phone number =======================

@DEFERRED_WEB
@pytest.mark.parametrize("source", ["web", "ats"])
def test_a_session_created_with_the_phone_number_continues_on_whatsapp(channel_stack, source):
    """The web chat (asking the number up front) or an ATS (which has it) pass the candidate's phone:
    when the candidate then writes on WhatsApp, it is the same screening."""
    stack = channel_stack()
    created = stack.api.post("/api/sessions", json={"profile_id": "delivery_courier", "source": source,
                                                    "contact": "+34 600 111 222"}).json()
    for text in courier()[:2]:
        stack.api.post(f"/api/sessions/{created['session_id']}/messages", json={"text": text})

    replies = stack.sim.phone(LAURA).whatsapp("hola, sigo por aquí")

    assert replies and not is_opening(replies[0]), f"WhatsApp started a second screening: {replies!r}"
    assert [r["id"] for r in sessions_for(LAURA)] == [created["session_id"]]


# ======================= §2A: Mexican numbers, as each provider writes them =======================

def test_a_mexican_whatsapp_candidate_continues_on_the_phone(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    whatsapp_line = stack.sim.phone("+5215512345678")  # WhatsApp's id for the line (legacy "1")
    whatsapp_line.whatsapp("hola")
    for text in courier("José Pérez Luna")[:2]:
        whatsapp_line.whatsapp(text)

    call = stack.sim.phone("+525512345678").call()  # the same line's caller ID

    assert not is_opening(call.prompt), f"the call started a second screening: {call.prompt!r}"
    assert len(sessions_for("+5215512345678", "+525512345678")) == 1


# ======================= §4: provider retries of phone webhooks =======================

def _signed_post(stack, url: str, params: dict[str, str], token: str):
    """Post a Twilio webhook exactly as Twilio retries it: same URL, body, signature and idempotency token."""
    headers = {"I-Twilio-Idempotency-Token": token, "X-Twilio-Signature": stack.sim.twilio.signature(url, params)}
    return stack.api.post(url, data=params, headers=headers)


def _call_params(stack, call_sid: str, number: str = LAURA, **extra: str) -> dict[str, str]:
    return {"AccountSid": stack.sim.twilio.account_sid, "CallSid": call_sid, "CallStatus": "ringing",
            "Direction": "inbound", "From": number, "To": stack.sim.voice_number, **extra}


def test_a_replayed_incoming_call_webhook_is_the_same_call(channel_stack):
    stack = channel_stack(voice="twilio")
    url = f"{settings.public_base_url}/api/channels/voice/inbound"
    params = _call_params(stack, "CA" + "1" * 32)

    first = _signed_post(stack, url, params, token="retry-token-1").text
    retry = _signed_post(stack, url, params, token="retry-token-1").text

    session_id = re.search(r"session_id=([0-9a-f-]+)", first).group(1)
    assert re.search(r"session_id=([0-9a-f-]+)", retry).group(1) == session_id
    assert retry == first, "the retry was answered as a call-back"
    assert event_types(session_id).count("voice_call_started") == 1


def test_a_replayed_speech_result_is_processed_once(channel_stack):
    """Twilio re-posts the <Gather> result when our answer is late: the caller said it once."""
    stack = channel_stack(voice="twilio")
    call = stack.sim.phone(LAURA).call()
    for text in courier()[:2]:
        call.say(text)  # now at the licence question
    session_id = stack.session_for(LAURA)["id"]
    twiml = [e for e in stack.sim.log if e.kind == "webhook" and "/voice/" in e.url][-1].response
    action = re.search(r'<Gather[^>]*action="([^"]+)"', twiml).group(1).replace("&amp;", "&")
    url = action if action.startswith("http") else f"{settings.public_base_url}{action}"
    params = _call_params(stack, call.sid, CallStatus="in-progress", SpeechResult="Sí, tengo carnet.",
                          Confidence="0.93")

    _signed_post(stack, url, params, token="gather-token-1")
    _signed_post(stack, url, params, token="gather-token-1")

    assert user_lines(session_id).count("Sí, tengo carnet.") == 1
    assert stack.session(session_id)["stage"] == "location"


# ======================= analytics: people, not sessions =======================

@DEFERRED_ANALYTICS
def test_the_backoffice_analytics_count_one_candidate_for_two_applications(channel_stack):
    stack = channel_stack(voice_profile="construction_laborer")
    laura = stack.sim.phone(LAURA)
    laura.whatsapp("hola")
    for text in courier()[:3]:
        laura.whatsapp(text)
    call = laura.call()
    for text in construction("Laura Jiménez Ortiz")[:2]:
        call.say(text)
    call.hangup()

    rows = sessions_for(LAURA)
    assert sorted(r["profile_id"] for r in rows) == ["construction_laborer", "delivery_courier"]
    assert_same_candidate(*[r["id"] for r in rows])
    assert total_candidates(stack.api.get("/api/bo/analytics").json()) == 1


# ======================= §3-E6: retention =======================

@DEFERRED_ANALYTICS
def test_screenings_past_the_retention_period_are_purged_while_the_app_runs(tmp_path, monkeypatch):
    """RETENTION_DAYS is documented (90 days, GDPR/LOPD) but nothing ever calls the purge."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "retention.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "reengage_tick_seconds", 0.05)
    db.init_db()
    expired = (datetime.now(timezone.utc) - timedelta(days=settings.retention_days + 10)).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO sessions (id, profile_id, status, stage, started_at, last_ts) VALUES (?,?,?,?,?,?)",
                     ("expired-session", "delivery_courier", "abandoned", "abandoned", expired, expired))
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app):
        deadline = time.monotonic() + 3
        while db.get_session("expired-session") is not None and time.monotonic() < deadline:
            time.sleep(0.05)

    assert db.get_session("expired-session") is None, "a screening past the retention period is still stored"
