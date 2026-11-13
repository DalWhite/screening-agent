"""Test suite layout (markers come from the directory, see ``pytest_collection_modifyitems``):

    tests/unit/          one component, everything else faked            marker: unit
    tests/integration/   the whole app in-process (TestClient), channels
                         through the channel simulator                   marker: integration
    tests/e2e/           live servers + a real browser (Playwright)      marker: e2e (+ channels)
    tests/stress/        load and resource usage (opt-in)                marker: stress
    tests/evals/         scripted conversation evals (a script, not pytest)
    tests/*/identity/    candidate identity & idempotency scenarios    marker: identity (+ layer)
    tests/support/       helpers shared by the layers (importable)

``python -m pytest`` runs unit + integration; ``-m e2e`` / ``-m stress`` the others.

This file: the in-process fixtures used by unit and integration tests. Everything
external is faked: the LLM (MockLLM), Twilio's REST client (FakeTwilioClient) or the
whole channel side (the channel simulator), and the database (a per-test SQLite file).
"""

import importlib.util
from pathlib import Path

from types import SimpleNamespace

import pytest

from backend.integrations.twilio.client import TwilioClient
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


TESTS = Path(__file__).resolve().parent
LAYERS = ("unit", "integration", "e2e", "stress")

# the browser layers need the dev requirements (pytest-playwright); unit/integration do not
if importlib.util.find_spec("playwright") is None:
    collect_ignore = ["e2e", "stress"]


def pytest_collection_modifyitems(config, items):
    """Mark every test with its layer (its top folder) and the channel e2e tests with ``channels``."""
    for item in items:
        parts = Path(str(item.fspath)).resolve().relative_to(TESTS).parts
        if parts and parts[0] in LAYERS:
            item.add_marker(getattr(pytest.mark, parts[0]))
            if parts[0] == "e2e" and len(parts) > 2 and parts[1] == "channels":
                item.add_marker(pytest.mark.channels)
            if "identity" in parts[1:-1]:  # tests/unit/identity, tests/integration/identity
                item.add_marker(pytest.mark.identity)


@pytest.fixture(autouse=True)
def _fresh_profile_registry(request):
    """Profiles published by one test (through the app's profile store) never leak into the next
    (in-process tests only: e2e and stress tests talk to servers in other processes)."""
    if request.node.get_closest_marker("e2e") or request.node.get_closest_marker("stress"):
        yield
        return
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
    return TwilioClient(config=twilio_config, client=fake_twilio_client)


@pytest.fixture
def channel_stack(tmp_path, monkeypatch):
    """Factory for the full app (TestClient, MockLLM) wired to the Twilio simulator.

    ``stack = channel_stack(whatsapp_profile="construction_laborer")`` applies the
    settings overrides, boots the app and returns a ``simulators.testing.InProcessStack``.
    ``provider="meta"`` runs WhatsApp on the (simulated) Meta Cloud API instead of Twilio,
    ``voice="vonage"`` the phone line on the (simulated) Vonage Voice API.
    Settings are restored after the test.
    """
    from simulators.testing import APP_SETTING_KEYS, InProcessStack

    stacks = []

    def make(sim_kwargs=None, provider="twilio", voice="twilio", **overrides):
        monkeypatch.setattr(settings, "db_path", str(tmp_path / f"channels{len(stacks)}.db"))
        for key in {*APP_SETTING_KEYS, *overrides}:
            monkeypatch.setattr(settings, key, getattr(settings, key))  # registers the restore
        stack = InProcessStack(overrides, whatsapp_provider=provider, voice_provider=voice,
                               **(sim_kwargs or {})).__enter__()
        stacks.append(stack)
        return stack

    yield make
    for stack in reversed(stacks):
        stack.__exit__(None, None, None)


# The provider matrix comes from the backend registry: registering a provider (with its simulator
# plugin) makes every test below run against it. See docs/channel_architecture.md.
from backend.integrations.registry import providers as _registered  # noqa: E402


@pytest.fixture(params=_registered("whatsapp"))
def wa_provider(request):
    """WhatsApp provider under test: every test using it runs once per registered provider."""
    return request.param


@pytest.fixture(params=_registered("voice"))
def voice_provider(request):
    """Voice provider under test: every test using it runs once per registered provider."""
    return request.param
