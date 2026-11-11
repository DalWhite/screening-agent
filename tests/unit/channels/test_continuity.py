"""Building blocks of channel continuity (the stories live in tests/integration/channels/test_stories.py)."""

import json

import pytest

from backend.backoffice.service import _clean_contact
from backend.integrations.twilio.client import BaseUrlHttpClient
from backend.session_store import SessionStore
from backend.storage import db


@pytest.mark.parametrize("raw, stored", [
    ("+34 600 111 222", "+34600111222"),
    ("+34-600-111-222", "+34600111222"),
    ("(+52) 55 1234 5678", "+525512345678"),
    ("laura@example.com", "laura@example.com"),
    ("  ", None),
    (None, None),
])
def test_recruiter_contacts_are_stored_like_channels_address_them(raw, stored):
    assert _clean_contact(raw) == stored


def test_state_tracks_the_channels_a_candidate_used(agent):
    state = agent.new_session(source="whatsapp", contact="+34600111222")
    assert state.channels == ["whatsapp"] and state.current_channel == "whatsapp"
    assert state.use_channel("whatsapp") is None  # same channel: not a switch
    assert state.use_channel("voice") == "whatsapp"
    assert state.channels == ["whatsapp", "voice"] and state.current_channel == "voice"
    assert state.reachable_on("whatsapp") and state.reachable_on("voice") and not state.reachable_on("web")
    assert state.use_channel("whatsapp") == "voice" and state.channels == ["whatsapp", "voice"]


def test_channels_survive_a_restart(agent, temp_db):
    state = agent.new_session(source="voice", contact="+34600111222")
    state.use_channel("whatsapp")
    db.save_session(state)
    restored = SessionStore().get(state.id)
    assert restored.channels == ["voice", "whatsapp"] and restored.current_channel == "whatsapp"
    assert db.get_session(state.id)["channels"] == ["voice", "whatsapp"]


def test_sessions_saved_before_channel_continuity_default_to_their_source(agent, temp_db):
    state = agent.new_session(source="whatsapp", contact="+34600111222")
    db.save_session(state)
    with db.get_conn() as conn:  # what an older release wrote into state_json
        conn.execute("UPDATE sessions SET state_json=? WHERE id=?",
                     (json.dumps({"attempts": {}, "abuse_count": 0, "completed_stages": []}), state.id))
    assert db.get_session(state.id)["channels"] == ["whatsapp"]
    restored = SessionStore().get(state.id)
    assert restored.channels == ["whatsapp"] and restored.reachable_on("whatsapp")


def test_live_lookup_is_scoped_to_one_process_and_undecided_sessions(agent, temp_db):
    def session(profile_id, status, source="whatsapp"):
        state = agent.new_session(source=source, contact="+34600111222", profile_id=profile_id)
        state.status = status
        db.save_session(state)
        return state.id

    live = session("construction_laborer", "active", source="voice")
    session("construction_laborer", "qualified")
    session("delivery_courier", "active")
    found = [r["id"] for r in db.find_live_sessions_by_contact("+34600111222", "construction_laborer")]
    assert found == [live]


def test_base_url_client_sends_twilio_requests_to_the_simulator(monkeypatch):
    seen = []
    monkeypatch.setattr("twilio.http.http_client.TwilioHttpClient.request",
                        lambda self, method, url, *a, **kw: seen.append((method, url)))
    client = BaseUrlHttpClient("http://127.0.0.1:8900/")
    client.request("POST", "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json")
    client.request("GET", "https://api.twilio.com/2010-04-01/Accounts/AC1.json?x=1")
    client.request("GET", "https://example.com/other")  # not Twilio: untouched
    assert seen == [("POST", "http://127.0.0.1:8900/2010-04-01/Accounts/AC1/Messages.json"),
                    ("GET", "http://127.0.0.1:8900/2010-04-01/Accounts/AC1.json?x=1"),
                    ("GET", "https://example.com/other")]
