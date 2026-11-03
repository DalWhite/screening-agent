"""Voice channel tests.

Twilio Voice is exercised as it really behaves: the channel returns TwiML and
Twilio's STT result comes back as plain text (`SpeechResult`). No telephony,
no audio, no network — the agent only ever sees text either way.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from backend.channels.base import strip_for_voice
from backend.channels.voice import VoiceChannel
from backend.config import settings
from backend.screening.schema import Stage, TurnDecision
from backend.session_store import SessionStore
from backend.storage import db

CALLER = "+34600111333"

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
def channel(agent, store):
    return VoiceChannel(agent, store)


def session_id_of(twiml: str) -> str:
    return re.search(r"session_id=([0-9a-f-]+)", twiml).group(1)


def said(twiml: str) -> str:
    return " ".join(re.findall(r"<Say[^>]*>(.*?)</Say>", twiml, flags=re.S))


# ---------- call flow ----------

def test_incoming_call_greets_and_gathers_speech(channel):
    twiml = channel.handle_call_start(CALLER, "CA001")
    assert '<Gather action="/api/channels/voice/collect?session_id=' in twiml
    assert 'input="speech"' in twiml
    assert 'language="es-ES"' in twiml
    assert "Grupo Sazón" in said(twiml)
    # no-input safety net: redirect back into collect
    assert "<Redirect" in twiml

    sid = session_id_of(twiml)
    row = db.get_session(sid)
    assert row["source"] == "voice"
    assert row["contact"] == CALLER
    assert channel.calls["CA001"] == sid


def test_full_qualified_screening_over_voice(channel):
    twiml = channel.handle_call_start(CALLER, "CA002")
    sid = session_id_of(twiml)
    for text in QUALIFIED_SCRIPT[:-1]:
        twiml = channel.handle_speech(sid, text)
        assert "<Gather" in twiml and said(twiml).strip()
    twiml = channel.handle_speech(sid, QUALIFIED_SCRIPT[-1])

    # terminal turn: speak the outcome, no further gather, hang up
    assert "<Gather" not in twiml
    assert "<Hangup" in twiml
    row = db.get_session(sid)
    assert row["stage"] == Stage.QUALIFIED.value
    assert row["summary"]["qualified"] is True


def test_no_license_disqualifies_and_hangs_up(channel):
    twiml = channel.handle_call_start(CALLER, "CA003")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "hola, quiero el trabajo")
    channel.handle_speech(sid, "Pedro Sánchez")
    twiml = channel.handle_speech(sid, "no tengo carnet")
    assert "<Hangup" in twiml and "<Gather" not in twiml
    assert db.get_session(sid)["summary"]["disqualify_reason"] == "no_license"


def test_language_switch_switches_tts_and_stt_locale(channel):
    twiml = channel.handle_call_start(CALLER, "CA004")
    sid = session_id_of(twiml)
    twiml = channel.handle_speech(sid, "Hi, I want to apply for the courier job")
    assert 'language="en-US"' in twiml  # both <Gather> and <Say> follow the candidate


# ---------- in-call silence ----------

def test_silence_reprompts_with_incremented_retry(channel):
    twiml = channel.handle_call_start(CALLER, "CA005")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "hola")

    twiml = channel.handle_speech(sid, "", retries=0)
    assert "retries=1" in twiml
    assert "<Gather" in twiml
    assert "¿Sigues ahí?" in said(twiml)


def test_silence_exhausted_closes_session_as_abandoned(channel):
    twiml = channel.handle_call_start(CALLER, "CA006")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "hola")

    twiml = channel.handle_speech(sid, "", retries=settings.voice_max_silence_retries)
    assert "<Hangup" in twiml and "<Gather" not in twiml
    row = db.get_session(sid)
    assert row["status"] == "abandoned"
    assert "silence" in row["flags"]


# ---------- resume / call back ----------

def test_calling_back_resumes_abandoned_session(channel, store):
    twiml = channel.handle_call_start(CALLER, "CA007")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "Hola, quiero aplicar para repartidor")
    state = store.get(sid)
    decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
    channel.agent._close(state, decision, Stage.ABANDONED, "silence", None)
    db.save_session(state)

    twiml = channel.handle_call_start(CALLER, "CA008")
    assert session_id_of(twiml) == sid  # same screening, picked up where it stopped
    assert "<Gather" in twiml
    assert state.status == "active"


def test_calling_back_into_active_session_reasks_current_question(channel, store):
    twiml = channel.handle_call_start(CALLER, "CA009")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "Hola, quiero aplicar para repartidor")  # now at NAME

    twiml = channel.handle_call_start(CALLER, "CA010")  # dropped call, rings again
    assert session_id_of(twiml) == sid
    assert "nombre" in said(twiml).lower()  # re-asks the pending question


def test_expired_session_gets_fresh_call(channel, store):
    twiml = channel.handle_call_start(CALLER, "CA011")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "hola")
    state = store.get(sid)
    decision = TurnDecision(stage_before=state.stage, stage_after=state.stage)
    channel.agent._close(state, decision, Stage.ABANDONED, "silence", None)
    state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(hours=settings.resume_window_hours + 1)
    db.save_session(state)

    twiml = channel.handle_call_start(CALLER, "CA012")
    assert session_id_of(twiml) != sid


# ---------- edge cases ----------

def test_unknown_session_apologises_and_hangs_up(channel):
    twiml = channel.handle_speech("00000000-0000-0000-0000-000000000000", "hola")
    assert "<Hangup" in twiml and "<Gather" not in twiml


def test_call_status_callback_records_event(channel):
    twiml = channel.handle_call_start(CALLER, "CA013")
    sid = session_id_of(twiml)
    channel.handle_call_ended("CA013", "completed")
    events = db.get_session(sid)["events"]
    assert any(e["type"] == "voice_call_ended" and e["payload"]["status"] == "completed" for e in events)
    assert "CA013" not in channel.calls
    channel.handle_call_ended("CA-unknown", "completed")  # must not raise


def test_speech_not_gathered_after_final_outcome(channel):
    twiml = channel.handle_call_start(CALLER, "CA014")
    sid = session_id_of(twiml)
    channel.handle_speech(sid, "hola")
    channel.handle_speech(sid, "Pedro Sánchez")
    channel.handle_speech(sid, "no tengo carnet")  # disqualified
    twiml = channel.handle_speech(sid, "pero espera")  # Twilio may still post once more
    assert "<Hangup" in twiml and "<Gather" not in twiml


# ---------- helpers ----------

def test_strip_for_voice_removes_emoji_and_markdown():
    assert strip_for_voice("¡Genial! 🎉 **Siguiente** paso:\nturno de *mañana*") == \
        "¡Genial! Siguiente paso: turno de mañana"
