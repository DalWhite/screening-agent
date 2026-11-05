"""Human takeover across channels: WhatsApp push, silent agent, voice, re-engagement."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from backend.backoffice.service import BackofficeService
from backend.channels.voice import VoiceChannel
from backend.channels.whatsapp import WhatsAppChannel
from backend.config import settings
from backend.reengagement import ReengagementMonitor
from backend.storage import db

CONTACT = "whatsapp:+34600999888"
NUMBER = "+34600999888"


@pytest.fixture
def whatsapp(agent, store, provider):
    return WhatsAppChannel(agent, store, provider, profile_id="construction_laborer")


@pytest.fixture
def bo(agent, store, whatsapp):
    return BackofficeService(agent, store, notifier=whatsapp.send_outbound)


def messages_in(twiml: str) -> list[str]:
    return re.findall(r"<Message>(.*?)</Message>", twiml, flags=re.S)


def test_whatsapp_number_runs_the_configured_profile(whatsapp):
    greeting = messages_in(whatsapp.handle_inbound(CONTACT, "hola"))[0]
    assert "Constructora Horizonte" in greeting
    row = db.find_session_by_contact("whatsapp", NUMBER)
    assert row["profile_id"] == "construction_laborer"


def test_recruiter_message_is_pushed_over_whatsapp_and_agent_stays_silent(whatsapp, bo, fake_twilio_client):
    whatsapp.handle_inbound(CONTACT, "hola")
    whatsapp.handle_inbound(CONTACT, "hola, quiero trabajar")
    sid = db.find_session_by_contact("whatsapp", NUMBER)["id"]

    result = bo.send_message(sid, "Hola, soy Ana de RRHH. ¿Hablamos?", by="Ana")
    assert result == {"id": sid, "delivered": True, "bot_paused": True}
    sent = fake_twilio_client.messages.sent[-1]
    assert sent["to"] == f"whatsapp:{NUMBER}" and "soy Ana" in sent["body"]

    # the candidate answers the recruiter: no automatic reply in the TwiML
    assert messages_in(whatsapp.handle_inbound(CONTACT, "Sí, claro")) == []
    transcript = db.get_session(sid)["messages"]
    assert [m["role"] for m in transcript[-2:]] == ["recruiter", "user"]
    assert transcript[-2]["author"] == "Ana" and transcript[-2]["delivered"] == 1

    # hand back: the re-asked question is pushed, then the agent answers again
    back = bo.takeover(sid, paused=False, by="Ana")
    assert "Sigo yo" in back["message"] or "Gracias por esperar" in back["message"]
    assert "Gracias por esperar" in fake_twilio_client.messages.sent[-1]["body"]
    reply = messages_in(whatsapp.handle_inbound(CONTACT, "Carlos Martín Ruiz"))
    assert reply and "18" in reply[0]


def test_failed_push_keeps_the_message_queued(whatsapp, bo, fake_twilio_client):
    whatsapp.handle_inbound(CONTACT, "hola")
    sid = db.find_session_by_contact("whatsapp", NUMBER)["id"]
    fake_twilio_client.messages.fail = True
    result = bo.send_message(sid, "¿Sigues interesado?", by="Ana")
    assert result["delivered"] is False
    queued = db.pending_messages(sid)
    assert queued and queued[0]["role"] == "recruiter"


def test_voice_caller_is_told_a_recruiter_will_answer(agent, store):
    voice = VoiceChannel(agent, store)
    twiml = voice.handle_call_start("+34600111444", "CA900")
    sid = re.search(r"session_id=([0-9a-f-]+)", twiml).group(1)
    state = store.get(sid)
    state.bot_paused = True
    twiml = voice.handle_speech(sid, "hola, ¿hay alguien?")
    assert "reclutador" in twiml and "<Gather" in twiml


def test_reengagement_monitor_leaves_taken_over_sessions_alone(agent, store):
    state = agent.new_session(profile_id="construction_laborer")
    agent.opening(state)
    agent.process(state, "hola")
    state.bot_paused = True
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(seconds=settings.session_idle_seconds + 60)
    store.put(state)
    db.save_session(state)
    monitor = ReengagementMonitor(agent, store.registry)
    monitor.tick()
    assert state.reengage_attempts == 0 and state.status == "active"
    state.bot_paused = False
    monitor.tick()
    assert state.reengage_attempts == 1


def test_takeover_survives_a_restart(agent, temp_db):
    from backend.session_store import SessionStore

    first = SessionStore()
    state = agent.new_session(profile_id="construction_laborer")
    agent.opening(state)
    for text in ["hola", "Carlos Martín Ruiz", "sí"]:
        agent.process(state, text)
    state.bot_paused = True
    first.put(state)
    db.save_session(state)

    restored = SessionStore().get(state.id)  # empty registry: rehydrated from SQLite
    assert restored.bot_paused is True
    assert restored.profile_id == "construction_laborer" and restored.stage == "work_permit"
    assert restored.completed_stages == state.completed_stages
    reply, decision = agent.process(restored, "¿seguimos?")
    assert reply == "" and decision.paused
