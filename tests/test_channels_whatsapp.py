"""WhatsApp channel tests.

Twilio is never contacted: inbound messages are handed straight to
`WhatsAppChannel.handle_inbound` (what our webhook does after signature
validation) and outbound pushes go through the FakeTwilioClient fixture.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from backend.channels.base import normalize_contact, split_message
from backend.channels.twilio_provider import TwilioProvider
from backend.channels.whatsapp import WhatsAppChannel
from backend.config import settings
from backend.reengagement import ReengagementMonitor
from backend.screening.schema import Stage
from backend.session_store import SessionStore
from backend.storage import db

CONTACT = "whatsapp:+34600111222"
NUMBER = "+34600111222"

QUALIFIED_SCRIPT = [
    "Hola, quiero aplicar para repartidor",
    "Laura Jiménez Ortiz",
    "sí, tengo carnet",
    "Madrid, Malasaña",
    "jornada completa",
    "mañana",
    "2 años en Glovo",
    "puedo empezar en 2 semanas",
]


@pytest.fixture
def channel(agent, store, provider):
    return WhatsAppChannel(agent, store, provider)


def messages_in(twiml: str) -> list[str]:
    return re.findall(r"<Message>(.*?)</Message>", twiml, flags=re.S)


# ---------- inbound conversation flow ----------

def test_first_message_opens_session_with_greeting(channel):
    twiml = channel.handle_inbound(CONTACT, "hola")
    msgs = messages_in(twiml)
    assert twiml.startswith("<?xml")
    assert len(msgs) == 1 and "Grupo Sazón" in msgs[0]

    row = db.find_session_by_contact("whatsapp", NUMBER)
    assert row is not None
    assert row["source"] == "whatsapp"
    assert row["contact"] == NUMBER
    assert row["status"] == "active"
    # trigger message is kept for the transcript
    session = db.get_session(row["id"])
    assert [m["role"] for m in session["messages"]] == ["assistant", "user"]
    assert session["messages"][1]["content"] == "hola"


def test_full_qualified_screening_over_whatsapp(channel):
    channel.handle_inbound(CONTACT, "hola")
    replies = []
    for text in QUALIFIED_SCRIPT:
        twiml = channel.handle_inbound(CONTACT, text)
        msgs = messages_in(twiml)
        assert msgs and all(m.strip() for m in msgs)
        replies.extend(msgs)

    row = db.find_session_by_contact("whatsapp", NUMBER)
    assert row["stage"] == Stage.QUALIFIED.value
    assert row["summary"]["qualified"] is True
    assert row["data"]["city"] == "Madrid"
    # full transcript persisted: trigger + 8 turns + assistant replies
    session = db.get_session(row["id"])
    assert sum(1 for m in session["messages"] if m["role"] == "user") == 1 + len(QUALIFIED_SCRIPT)


def test_same_number_continues_same_session_across_restarts(agent, provider):
    first = WhatsAppChannel(agent, SessionStore(), provider)
    first.handle_inbound(CONTACT, "hola")
    first.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    session_id = db.find_session_by_contact("whatsapp", NUMBER)["id"]

    # process restart: fresh in-memory store, same database
    second = WhatsAppChannel(agent, SessionStore(), provider)
    twiml = second.handle_inbound(CONTACT, "Laura Jiménez Ortiz")
    assert messages_in(twiml)
    assert db.find_session_by_contact("whatsapp", NUMBER)["id"] == session_id


def test_abandoned_session_resumes_within_window(channel, store):
    channel.handle_inbound(CONTACT, "hola")
    channel.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    state = store.find_by_contact("whatsapp", NUMBER)
    session_id = state.id

    # simulate the silence monitor closing it as abandoned
    from backend.screening.schema import TurnDecision
    decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
    channel.agent._close(state, decision, Stage.ABANDONED, "silence", None)
    db.save_session(state)

    twiml = channel.handle_inbound(CONTACT, "perdona, ya estoy aquí")
    msgs = messages_in(twiml)
    assert len(msgs) >= 2  # resume greeting + next question
    assert state.status == "active"
    assert db.find_session_by_contact("whatsapp", NUMBER)["id"] == session_id


def test_expired_abandoned_session_starts_fresh(channel, store):
    channel.handle_inbound(CONTACT, "hola")
    channel.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    state = store.find_by_contact("whatsapp", NUMBER)
    old_id = state.id
    from backend.screening.schema import TurnDecision
    decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
    channel.agent._close(state, decision, Stage.ABANDONED, "silence", None)
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(hours=settings.resume_window_hours + 2)
    db.save_session(state)

    twiml = channel.handle_inbound(CONTACT, "hola de nuevo")
    msgs = messages_in(twiml)
    assert len(msgs) == 1 and "Grupo Sazón" in msgs[0]  # new opening, not a resume
    new_row = db.find_session_by_contact("whatsapp", NUMBER)
    assert new_row["id"] != old_id


def test_final_decision_is_final_new_conversation_starts(channel):
    channel.handle_inbound(CONTACT, "hola")
    for text in QUALIFIED_SCRIPT:
        channel.handle_inbound(CONTACT, text)
    qualified_id = db.find_session_by_contact("whatsapp", NUMBER)["id"]

    twiml = channel.handle_inbound(CONTACT, "hola otra vez")
    assert "Grupo Sazón" in messages_in(twiml)[0]
    assert db.find_session_by_contact("whatsapp", NUMBER)["id"] != qualified_id


def test_no_license_disqualified_over_whatsapp(channel):
    for text in ["hola", "quiero el trabajo", "Pedro Sánchez", "no tengo carnet"]:
        twiml = channel.handle_inbound(CONTACT, text)
    row = db.find_session_by_contact("whatsapp", NUMBER)
    assert row["stage"] == Stage.DISQUALIFIED.value
    assert row["summary"]["disqualify_reason"] == "no_license"
    assert messages_in(twiml)


# ---------- outbound / re-engagement ----------

def test_outbound_nudge_sent_through_twilio(channel, store, fake_twilio_client):
    channel.handle_inbound(CONTACT, "hola")
    channel.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    state = store.find_by_contact("whatsapp", NUMBER)
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(seconds=settings.reengage_first_delay + 5)

    def notifier(s, text):
        return channel.send_outbound(s, text)

    monitor = ReengagementMonitor(channel.agent, store.registry, notifier=notifier)
    monitor.tick()

    assert len(fake_twilio_client.messages.sent) == 1
    sent = fake_twilio_client.messages.sent[0]
    assert sent["to"] == f"whatsapp:{NUMBER}"
    assert sent["from_"] == "whatsapp:+14155238886"
    assert sent["body"].strip()
    # pushed nudges are stored as delivered -> nothing pending
    assert db.pending_messages(state.id) == []


def test_nudge_queued_when_twilio_unconfigured(agent, store, monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    channel = WhatsAppChannel(agent, store, TwilioProvider(config=settings))
    channel.handle_inbound(CONTACT, "hola")
    channel.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    state = store.find_by_contact("whatsapp", NUMBER)
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(seconds=settings.reengage_first_delay + 5)

    monitor = ReengagementMonitor(agent, store.registry, notifier=channel.send_outbound)
    monitor.tick()  # must not raise

    pending = db.pending_messages(state.id)
    assert len(pending) == 1  # stored undelivered, picked up on next inbound


def test_outbound_failure_is_swallowed(channel, store, fake_twilio_client):
    channel.handle_inbound(CONTACT, "hola")
    state = store.find_by_contact("whatsapp", NUMBER)
    fake_twilio_client.messages.fail = True
    assert channel.send_outbound(state, "¿sigues ahí?") is False


def test_outbound_skips_non_whatsapp_sessions(channel, agent, fake_twilio_client):
    state = agent.new_session(source="web")
    assert channel.send_outbound(state, "hola") is False
    assert fake_twilio_client.messages.sent == []


# ---------- helpers ----------

def test_normalize_contact():
    assert normalize_contact("whatsapp:+34 600 111 222") == "+34600111222"
    assert normalize_contact("+34600111222") == "+34600111222"
    assert normalize_contact("") == ""


def test_split_message_respects_limit_and_keeps_content():
    text = "Primera frase corta. " * 40  # ~840 chars
    parts = split_message(text.strip(), 200)
    assert all(len(p) <= 200 for p in parts)
    assert " ".join(parts).replace("  ", " ") .replace(" ", "") == text.strip().replace(" ", "")


def test_split_message_short_text_passthrough():
    assert split_message("hola", 1500) == ["hola"]
    assert split_message("   ", 1500) == []


# ---------- webhook deadline: slow LLM turns are delivered via REST ----------

def _slow_process(agent, delay):
    original = agent.process

    def slow(state, text, today=None):
        import time
        time.sleep(delay)
        return original(state, text, today=today)

    return slow


def test_fast_turn_replies_inline_with_twiml(channel):
    async def run():
        return await channel.handle_inbound_async(CONTACT, "hola", sync_timeout=5)

    twiml = asyncio.run(run())
    assert "Grupo Sazón" in messages_in(twiml)[0]
    assert channel.late_deliveries == set()


def test_slow_turn_returns_empty_twiml_then_pushes_reply(channel, fake_twilio_client, monkeypatch):
    channel.handle_inbound(CONTACT, "hola")  # session exists, next turn goes through the agent
    monkeypatch.setattr(channel.agent, "process", _slow_process(channel.agent, 0.3))

    async def run():
        twiml = await channel.handle_inbound_async(CONTACT, "Hola, quiero aplicar para repartidor", sync_timeout=0.05)
        assert messages_in(twiml) == []  # empty <Response/> inside the webhook budget
        assert len(channel.late_deliveries) == 1
        for _ in range(50):  # wait for the background turn + REST push
            await asyncio.sleep(0.05)
            if fake_twilio_client.messages.sent:
                break
        return twiml

    asyncio.run(run())
    assert len(fake_twilio_client.messages.sent) == 1
    sent = fake_twilio_client.messages.sent[0]
    assert sent["to"] == f"whatsapp:{NUMBER}"
    assert "nombre" in sent["body"].lower()  # the real agent reply, not a placeholder
    assert channel.late_deliveries == set()
    # the turn itself was fully persisted despite the early webhook response
    row = db.find_session_by_contact("whatsapp", NUMBER)
    assert row["stage"] == Stage.NAME.value


def test_slow_turn_without_twilio_config_logs_and_does_not_crash(agent, store, monkeypatch, caplog):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    channel = WhatsAppChannel(agent, store, TwilioProvider(config=settings))
    channel.handle_inbound(CONTACT, "hola")
    monkeypatch.setattr(agent, "process", _slow_process(agent, 0.2))

    async def run():
        await channel.handle_inbound_async(CONTACT, "quiero aplicar", sync_timeout=0.05)
        await asyncio.sleep(0.5)

    asyncio.run(run())
    assert any("could not be sent" in r.message for r in caplog.records)


def test_turns_for_same_contact_are_serialised(channel):
    """Two webhooks racing for the same number must not interleave agent turns."""
    import threading
    channel.handle_inbound(CONTACT, "hola")
    results = []

    def worker(text):
        results.append(channel.handle_inbound(CONTACT, text))

    threads = [threading.Thread(target=worker, args=(t,)) for t in
               ["Hola, quiero aplicar para repartidor", "Laura Jiménez Ortiz"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and all(messages_in(r) for r in results)
    session = db.get_session(db.find_session_by_contact("whatsapp", NUMBER)["id"])
    roles = [m["role"] for m in session["messages"]]
    assert roles == ["assistant", "user", "user", "assistant", "user", "assistant"]


# ---------- template fallback (error 21654 / 63016) ----------

def _session_with_name(channel, store):
    channel.handle_inbound(CONTACT, "hola")
    channel.handle_inbound(CONTACT, "Hola, quiero aplicar para repartidor")
    channel.handle_inbound(CONTACT, "Laura Jiménez Ortiz")
    return store.find_by_contact("whatsapp", NUMBER)


def test_nudge_falls_back_to_template_when_twilio_requires_one(channel, store, fake_twilio_client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_content_sid", "HXnudge")
    fake_twilio_client.messages.reject_freeform_with = 21654
    state = _session_with_name(channel, store)

    assert channel.send_outbound(state, "¿Sigues ahí?") is True
    assert len(fake_twilio_client.messages.sent) == 1
    sent = fake_twilio_client.messages.sent[0]
    assert sent["content_sid"] == "HXnudge"
    assert json.loads(sent["content_variables"]) == {"1": "Laura", "2": "Marisol"}
    events = db.get_session(state.id)["events"]
    assert any(e["type"] == "whatsapp_template_fallback" for e in events)


def test_nudge_template_fallback_outside_window(channel, store, fake_twilio_client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_content_sid", "HXnudge")
    fake_twilio_client.messages.reject_freeform_with = 63016
    state = _session_with_name(channel, store)
    assert channel.send_outbound(state, "¿Sigues ahí?") is True
    assert fake_twilio_client.messages.sent[0]["content_sid"] == "HXnudge"


def test_nudge_without_template_configured_fails_cleanly(channel, store, fake_twilio_client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "twilio_whatsapp_content_sid", "")
    fake_twilio_client.messages.reject_freeform_with = 21654
    state = _session_with_name(channel, store)
    assert channel.send_outbound(state, "¿Sigues ahí?") is False
    assert fake_twilio_client.messages.sent == []
    assert any("TWILIO_WHATSAPP_CONTENT_SID" in r.message for r in caplog.records)


def test_freeform_preferred_when_allowed(channel, store, fake_twilio_client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_content_sid", "HXnudge")
    state = _session_with_name(channel, store)
    assert channel.send_outbound(state, "¿Sigues ahí?") is True
    sent = fake_twilio_client.messages.sent[0]
    assert sent["body"] == "¿Sigues ahí?" and sent["content_sid"] is None


def test_template_variables_fallback_without_name(channel, store):
    channel.handle_inbound(CONTACT, "hola")
    state = store.find_by_contact("whatsapp", NUMBER)
    assert channel._template_variables(state) == {"1": "hola", "2": "Marisol"}
