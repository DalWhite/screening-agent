"""Robustness of the channel layer: ordering of pushes, restarts, provider retries, stale nudges.

Found by the live e2e suite (tests/e2e/channels/test_resilience.py); pinned here deterministically.
"""

from __future__ import annotations

import threading
import time

import pytest
from datetime import datetime, timedelta, timezone

from backend.config import settings
from backend.storage import db

LAURA = "+34600111222"


def started(stack, number=LAURA):
    phone = stack.sim.phone(number)
    for text in ["hola", "sí", "Laura Jiménez Ortiz"]:
        phone.whatsapp(text)
    return phone, stack.app.store.get(stack.session_for(phone)["id"])


# ---------- a push never overtakes the reply to the candidate's last message ----------

def test_a_message_waits_for_the_turn_that_is_still_being_answered(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    phone, state = started(stack)
    channel = stack.app.whatsapp_channel
    channel._begin(LAURA)  # a turn of Laura's is in flight (its reply is not out yet)
    sent = {}
    worker = threading.Thread(target=lambda: sent.setdefault("ok", channel.send_message(state, "¡Cumples!")))
    worker.start()
    time.sleep(0.3)
    assert "ok" not in sent and "¡Cumples!" not in phone.texts()  # held back...
    channel.release(LAURA)
    worker.join(timeout=5)
    assert sent["ok"] is True and phone.texts()[-1] == "¡Cumples!"  # ... and sent right after


def test_nudges_never_wait(channel_stack):
    """Nudges are sent from the event loop (the re-engagement monitor): waiting there would stall the app."""
    stack = channel_stack()
    phone, state = started(stack)
    channel = stack.app.whatsapp_channel
    channel._begin(LAURA)
    started_at = time.monotonic()
    assert channel.send_outbound(state, "¿Sigues ahí?") is True
    assert time.monotonic() - started_at < 1
    channel.release(LAURA)


def test_a_stuck_turn_delays_a_message_only_up_to_the_webhook_budget(channel_stack, monkeypatch):
    stack = channel_stack(whatsapp_sync_timeout=0.2)
    phone, state = started(stack)
    channel = stack.app.whatsapp_channel
    channel._begin(LAURA)  # never released (e.g. a provider that never acknowledges)
    try:
        assert channel.wait_until_idle(LAURA) is False  # gives up after sync timeout + 5s...
    finally:
        channel.release(LAURA)


# ---------- provider retries across restarts ----------

def test_inbound_claims_survive_a_restart(temp_db):
    assert db.claim_inbound("meta", "wamid.1") is True
    db.finish_inbound("meta", "wamid.1")
    assert db.claim_inbound("meta", "wamid.1") is False  # a new process sees the same database


def test_a_claim_left_by_a_crashed_process_is_taken_over(temp_db):
    assert db.claim_inbound("meta", "wamid.2") is True  # ... and the process dies mid-turn
    assert db.claim_inbound("meta", "wamid.2") is False  # a retry while it may still be running
    old = (datetime.now(timezone.utc) - timedelta(seconds=db.INBOUND_STALE_SECONDS + 5)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE channel_inbound SET received_at=? WHERE message_id='wamid.2'", (old,))
    assert db.claim_inbound("meta", "wamid.2") is True  # the redelivery is processed, not lost


def test_a_failed_turn_lets_the_retry_through(channel_stack, wa_provider, monkeypatch):
    stack = channel_stack(provider=wa_provider)
    phone, _ = started(stack)
    channel = stack.app.whatsapp_channel
    calls = {"n": 0}
    original = channel._process

    def flaky(message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return original(message)

    monkeypatch.setattr(channel, "_process", flaky)
    with pytest.raises(RuntimeError):  # in-process the 500 surfaces as the exception itself
        phone.whatsapp("sí, tengo carnet")  # the turn fails: the provider will retry
    assert "ciudad" in phone.resend_last()[0].lower()  # the retry is processed


def test_the_end_of_a_call_is_recorded_after_a_restart(channel_stack):
    from backend.channels.voice import VoiceChannel

    stack = channel_stack()
    call = stack.sim.phone(LAURA).call()
    call.say("sí")
    session_id = stack.session_for(LAURA)["id"]
    fresh = VoiceChannel(stack.app.agent, stack.app.store)  # a new process: empty CallSid map
    fresh.call_ended(call.sid, "completed")
    assert any(e["type"] == "voice_call_ended" for e in stack.session(session_id)["events"])


# ---------- stale nudges ----------

def test_nudges_a_caller_never_got_are_not_replayed_on_whatsapp(channel_stack):
    stack = channel_stack()
    phone = stack.sim.phone(LAURA)
    call = phone.call()
    call.say("sí")
    call.hangup()
    session_id = stack.session_for(phone)["id"]
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)  # stored: voice cannot push
    replies = phone.whatsapp("hola")
    assert not any("sigues" in r.lower() for r in replies) and "de nuevo" in replies[0].lower()
    nudge = next(m for m in stack.session(session_id)["messages"] if m["kind"] == "nudge")
    assert nudge["delivered"] == db.SUPERSEDED and nudge["channel"] is None


def test_recruiter_messages_are_still_delivered_when_nudges_are_dropped(channel_stack):
    stack = channel_stack()
    phone = stack.sim.phone(LAURA)
    call = phone.call()
    call.say("sí")
    call.hangup()
    session_id = stack.session_for(phone)["id"]
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "Laura, te llamo mañana", "pause_bot": False})
    replies = phone.whatsapp("hola")
    assert replies[0] == "Laura, te llamo mañana" and not any("sigues" in r.lower() for r in replies)


# ---------- delivery receipts processed concurrently ----------

def test_concurrent_receipts_only_move_a_delivery_forward(temp_db):
    """Regression: two receipts read "accepted" at once, "failed" was written, then "sent" overwrote it."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    for round_ in range(20):
        external_id = f"wamid.race{round_}"
        db.record_delivery(None, "whatsapp", "meta", external_id, "+34600", "message", "hola", "accepted")
        statuses = ["sent", "delivered", "failed", "sent", "delivered"]
        random.shuffle(statuses)
        with ThreadPoolExecutor(max_workers=len(statuses)) as pool:
            results = list(pool.map(lambda status: db.update_delivery("meta", external_id, status), statuses))
        with db.get_conn() as conn:
            final = conn.execute("SELECT status FROM channel_deliveries WHERE external_id=?", (external_id,)).fetchone()
        assert final["status"] == "failed"
        assert sum(1 for status, r in zip(statuses, results) if status == "failed" and r["applied"]) == 1
