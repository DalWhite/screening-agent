"""Channels wired like the app does, without HTTP: WhatsApp (fake Twilio client), phone, web, backoffice.

Every channel shares the test's ``store`` (one process). Tests that model several
workers build their own ``SessionStore`` per worker.
"""

import pytest

from backend.backoffice.service import BackofficeService
from backend.channels.messaging import MessagingChannel
from backend.channels.voice import VoiceChannel
from backend.channels.web import WebChannel


@pytest.fixture
def whatsapp(agent, store, provider):
    """The WhatsApp line (courier process)."""
    return MessagingChannel(agent, store, provider, profile_id="delivery_courier")


@pytest.fixture
def voice(agent, store):
    """The phone line (courier process)."""
    return VoiceChannel(agent, store, profile_id="delivery_courier")


@pytest.fixture
def lines(agent, store, provider):
    """Factory: a WhatsApp or phone line bound to any process (``lines("voice", "construction_laborer")``)."""
    def make(kind: str, profile_id: str, of_store=None):
        target = of_store or store
        if kind == "whatsapp":
            return MessagingChannel(agent, target, provider, profile_id=profile_id)
        return VoiceChannel(agent, target, profile_id=profile_id)
    return make


@pytest.fixture
def web(agent, store):
    return WebChannel(agent, store)


@pytest.fixture
def bo(agent, store, whatsapp):
    """Recruiter actions; pushes go out on WhatsApp like the hub's notifier does."""
    def notify(state, text):
        return whatsapp.source if whatsapp.send_message(state, text) else None
    return BackofficeService(agent, store, notifier=notify)
