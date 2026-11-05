"""Shared fixtures for channel tests.

Everything external is faked: the LLM (MockLLM), the Twilio REST client
(FakeTwilioClient) and the database (a per-test SQLite file). No test in this
suite opens a network connection.
"""

from types import SimpleNamespace

import pytest

from backend.channels.twilio_provider import TwilioProvider
from backend.config import settings
from backend.llm.client import MockLLM
from backend.screening.agent import ScreeningAgent
from backend.session_store import SessionStore
from backend.storage import db


class FakeMessages:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False
        self.reject_freeform_with: int | None = None  # e.g. 21654 -> free-form refused, templates accepted

    def create(self, to: str, from_: str, body: str | None = None, content_sid: str | None = None,
               content_variables: str | None = None):
        if self.fail:
            raise RuntimeError("twilio is down")
        if self.reject_freeform_with and not content_sid:
            from twilio.base.exceptions import TwilioRestException
            raise TwilioRestException(400, "/Messages.json", "ContentSid Required", self.reject_freeform_with)
        self.sent.append({"to": to, "from_": from_, "body": body, "content_sid": content_sid,
                          "content_variables": content_variables})
        return SimpleNamespace(sid=f"SM{len(self.sent):08d}")


class FakeTwilioClient:
    """Stands in for twilio.rest.Client — records outbound messages."""

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.messages = FakeMessages()


@pytest.fixture(autouse=True)
def _fresh_profile_registry():
    """Profiles published by one test (through the app's profile store) never leak into the next."""
    from backend.screening.profile import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    db.init_db()
    return tmp_path / "test.db"


@pytest.fixture
def agent(temp_db):
    return ScreeningAgent(MockLLM())


@pytest.fixture
def store(temp_db):
    return SessionStore()


@pytest.fixture
def twilio_config(monkeypatch):
    """Settings as if Twilio were fully configured (still no network: fake client)."""
    monkeypatch.setattr(settings, "twilio_account_sid", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-auth-token")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "whatsapp:+14155238886")
    monkeypatch.setattr(settings, "twilio_validate_signatures", False)
    return settings


@pytest.fixture
def fake_twilio_client():
    return FakeTwilioClient()


@pytest.fixture
def provider(twilio_config, fake_twilio_client):
    return TwilioProvider(config=twilio_config, client=fake_twilio_client)
