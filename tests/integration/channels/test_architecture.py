"""The channel architecture's rules, as tests (docs/channel_architecture.md).

1. The agent and the provider-neutral channel code never import a provider.
2. Every registered provider has a simulator plugin, so the contract tests
   (every test taking ``wa_provider`` / ``voice_provider``) run against it.
3. Adapters implement the adapter interface; channels are turned on, off and
   swapped by configuration only.
4. Adding a provider needs no change outside its adapter: a toy provider is
   registered here and screens a candidate.
"""

from __future__ import annotations

import ast
import hmac
import json
from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from backend.integrations import registry as provider_registry
from backend.channels.hub import ChannelHub
from backend.channels.contracts import InboundMessage, SendResult
from backend.integrations.registry import REGISTRY, ProviderSpec, register
from backend.channels.messaging import MessagingChannel
from backend.config import settings
from tests.support.paths import BACKEND

ROOT = BACKEND
VENDOR_SDKS = {"twilio", "vonage"}
VENDORS = sorted(p.name for p in (ROOT / "integrations").iterdir() if p.is_dir() and not p.name.startswith("_"))


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def touches_integrations(name: str) -> bool:
    """An import of vendor code: a vendor SDK or anything under backend/integrations."""
    bare = name.lstrip(".")
    return bare.split(".")[0] in VENDOR_SDKS or "integrations" in bare.split(".")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# the agent, the channels (except the hub, their composition root) and everything else in the core
CORE = sorted(rel(p) for p in ROOT.rglob("*.py")
              if not rel(p).startswith(("integrations/",)) and rel(p) not in ("channels/hub.py", "app.py"))


# ---------- 1. boundaries ----------

@pytest.mark.parametrize("module", CORE)
def test_the_core_never_imports_vendor_code(module):
    bad = {name for name in imports_of(ROOT / module) if touches_integrations(name)}
    assert not bad, f"{module} imports vendor code: {bad}"


@pytest.mark.parametrize("vendor", [v for v in VENDORS])
def test_a_vendor_package_never_imports_another_vendor(vendor):
    others = set(VENDORS) - {vendor}
    for path in (ROOT / "integrations" / vendor).glob("*.py"):
        bad = {n for n in imports_of(path) if set(n.lstrip(".").split(".")) & others}
        assert not bad, f"{rel(path)} imports another vendor: {bad}"


def test_the_app_only_knows_the_hub():
    names = imports_of(ROOT / "app.py")
    assert not {n for n in names if touches_integrations(n)}, names
    assert ".channels.hub" in names


# ---------- 2. every provider is covered by the simulator and the contract tests ----------

def test_every_registered_provider_has_a_simulator_plugin():
    from simulators.core import MESSAGING_PROVIDERS, VOICE_PROVIDERS

    assert set(provider_registry.providers("whatsapp")) <= set(MESSAGING_PROVIDERS)
    assert set(provider_registry.providers("voice")) <= set(VOICE_PROVIDERS)


def test_the_contract_matrix_is_the_registry():
    from tests.conftest import voice_provider, wa_provider

    assert list(wa_provider._fixture_function_marker.params) == provider_registry.providers("whatsapp")
    assert list(voice_provider._fixture_function_marker.params) == provider_registry.providers("voice")


# ---------- 3. adapters and configuration ----------

@pytest.mark.parametrize("kind, name", [(k, n) for k, specs in REGISTRY.items() for n in specs])
def test_adapters_implement_the_interface(agent, store, kind, name):
    adapter = ChannelHub(agent, store, selection={kind: name}).adapter(kind)
    assert adapter.name == name and adapter.kind == kind
    assert isinstance(adapter.router(), APIRouter) and adapter.router().routes
    assert isinstance(adapter.configured, bool)
    adapter.use_http(object())  # the test seam exists
    if kind == "whatsapp":
        assert isinstance(adapter.channel, MessagingChannel) and adapter.channel.source == "whatsapp"
        assert adapter.client.kind == "whatsapp"


def test_unknown_providers_fail_loudly(agent, store):
    with pytest.raises(ValueError, match="available"):
        ChannelHub(agent, store, selection={"voice": "carrier-pigeon"})


@pytest.fixture
def app_with(tmp_path, monkeypatch):
    def boot(**selection):
        monkeypatch.setattr(settings, "db_path", str(tmp_path / "arch.db"))
        monkeypatch.setattr(settings, "llm_fallback", "mock")
        for key, value in selection.items():
            monkeypatch.setattr(settings, key, value)
        import backend.app as appmod

        return TestClient(appmod.app)

    return boot


def test_channels_are_turned_off_and_swapped_by_configuration(app_with):
    with app_with(whatsapp_provider="none", voice_provider="vonage",
                  vonage_validate_signatures=False) as client:
        import backend.app as appmod

        assert set(appmod.hub.describe()) == {"web", "voice"} and appmod.whatsapp_channel is None  # web: always
        assert client.post("/api/channels/whatsapp/inbound", data={"From": "x"}).status_code in (404, 405)
        assert client.post("/api/channels/voice/inbound", data={"From": "x"}).status_code in (404, 405)
        ncco = client.get("/api/channels/voice/vonage/answer", params={"from": "34600111222", "uuid": "u1"}).json()
        assert ncco[0]["action"] == "talk" and ncco[1]["action"] == "input"
        meta = client.get("/api/bo/meta").json()["channels"]
        assert meta["voice"] == {"provider": "vonage", "label": {"es": "Teléfono", "en": "Phone"},
                                 "push": False, "configured": True}
        assert meta["web"]["provider"] == "builtin" and not meta["web"]["push"]
        assert client.post("/api/sessions", json={}).status_code == 200  # the web channel is always there
    with app_with(whatsapp_provider="twilio", voice_provider="twilio", twilio_validate_signatures=False) as client:
        assert client.post("/api/channels/voice/vonage/answer").status_code in (404, 405)  # swapped out
        assert "<Gather" in client.post("/api/channels/voice/inbound", data={"From": "+34600", "CallSid": "C"}).text


def test_routes_do_not_pile_up_across_restarts(app_with):
    import backend.app as appmod

    with app_with(whatsapp_provider="meta", voice_provider="twilio"):
        first = len(appmod.app.router.routes)
    with app_with(whatsapp_provider="meta", voice_provider="twilio"):
        assert len(appmod.app.router.routes) == first


# ---------- 4. adding a provider touches nothing else ----------

class ToyProvider:
    """A made-up messaging provider: JSON webhooks in, replies in the JSON answer, pushes recorded."""

    name = "toy"
    kind = "whatsapp"
    replies_in_webhook = True
    window_error_codes = frozenset()  # no service window
    template_setting = "-"
    configured = True

    def __init__(self):
        self.pushed: list[tuple[str, str]] = []

    def render_webhook_reply(self, replies):
        return json.dumps({"replies": replies})

    def send_text(self, to, body):
        self.pushed.append((to, body))
        return SendResult(ok=True, id=f"toy-{len(self.pushed)}")

    def template_configured(self):
        return False

    template_ref = ""

    def send_template(self, to, variables, language):
        return SendResult(ok=False)

    def fetch_media(self, media, max_bytes):
        raise NotImplementedError

    def mark_read(self, message_id):
        pass

    def use_http(self, client):
        pass


class ToyAdapter:
    name = "toy"
    kind = "whatsapp"

    def __init__(self, hub):
        self.client = ToyProvider()
        self.channel = MessagingChannel(hub.agent, hub.store, self.client, transcriber=hub.transcriber)
        self.configured = True

    def use_http(self, client):
        pass

    def router(self):
        router = APIRouter()

        @router.post("/api/channels/toy")
        async def toy(request: Request):
            data = await request.json()
            if not hmac.compare_digest(request.headers.get("x-toy-key", ""), "secret"):
                return {"error": "forbidden"}
            message = InboundMessage(contact=data["from"], text=data.get("text", ""), message_id=data.get("id"),
                                     provider="toy")
            return json.loads(self.client.render_webhook_reply(await self.channel.receive(message)))

        return router


@pytest.fixture
def toy_registered():
    register(ProviderSpec("whatsapp", "toy", ToyAdapter, "a provider written in a test"))
    yield
    REGISTRY["whatsapp"].pop("toy")


def test_a_new_provider_plugs_in_without_touching_the_core(app_with, toy_registered):
    import backend.app as appmod

    with app_with(whatsapp_provider="toy", voice_provider="none") as client:
        def say(text, id_):
            return client.post("/api/channels/toy", json={"from": "+34600111222", "text": text, "id": id_},
                               headers={"x-toy-key": "secret"}).json()["replies"]

        assert "Grupo Sazón" in say("hola", "1")[0]
        assert "nombre" in say("sí", "2")[0].lower()
        assert say("sí", "2") == []  # retries are deduplicated by the channel, whatever the provider
        session = client.get("/api/sessions", params={"q": "34600111222"}).json()["sessions"][0]
        assert session["source"] == "whatsapp"
        # outbound routing finds it too: a recruiter message is pushed through the toy provider
        sent = client.post(f"/api/bo/candidates/{session['id']}/messages", json={"text": "¡Hola!"}).json()
        assert sent["delivered"] is True
        assert appmod.hub.adapter("whatsapp").client.pushed[-1] == ("+34600111222", "¡Hola!")
