"""Fixtures for the channel e2e tests (WhatsApp / phone candidates through the channel simulator)."""

from __future__ import annotations

import pytest

from backend.integrations.registry import providers as registered
from tests.support.channels import CONSTRUCTION, StackPool


@pytest.fixture(scope="session")
def stacks(tmp_path_factory):
    pool = StackPool(tmp_path_factory)
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def channels(stacks):
    """WhatsApp through Twilio; both lines run the construction process."""
    return stacks.get("twilio", **CONSTRUCTION)


@pytest.fixture(scope="session")
def channels_meta(stacks):
    """Same as ``channels`` but WhatsApp goes straight through Meta's Cloud API (simulated)."""
    return stacks.get("meta", **CONSTRUCTION)


@pytest.fixture
def sim(stacks, channels):
    """The Twilio-mode simulator with Twilio behaving again (faults from a previous test cleared)."""
    return stacks.get("twilio", **CONSTRUCTION).sim


@pytest.fixture(params=registered("whatsapp"))
def live(request, stacks):
    """The construction stack for each registered WhatsApp provider: the test runs once per provider."""
    return stacks.get(request.param, **CONSTRUCTION)


@pytest.fixture(params=registered("voice"))
def live_voice(request, stacks):
    """The construction stack for each registered voice provider (WhatsApp on Twilio)."""
    return stacks.get("twilio", voice=request.param, **CONSTRUCTION)


