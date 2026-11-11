"""Which vendor integration serves which channel (``WHATSAPP_PROVIDER``, ``VOICE_PROVIDER``).

An integration's adapter wraps one provider for one channel kind and owns its
webhook routes (``router()``), request parsing, signature checks and answer
rendering (TwiML, NCCO, Graph API calls). It hands the provider-neutral channel
(`MessagingChannel` / `VoiceChannel`) neutral events and renders the neutral
answers it gets back, so the agent and the channels never see a wire format.

Adding a provider = ``integrations/<vendor>/`` (client + adapter) + an entry
here (+ a simulator plugin, so the channel contract tests run against it: see
docs/channel_architecture.md).

Adapter interface (duck-typed)::

    name: str                 # "twilio", "meta", "vonage"...
    kind: str                 # "whatsapp" | "voice"
    channel                   # the MessagingChannel / VoiceChannel it drives
    configured: bool          # credentials present
    router() -> APIRouter     # its webhooks
    extra_routers() -> {key: factory(hub) -> APIRouter}   # optional: shared routes, mounted once per key
    use_http(client)          # test seam: outbound HTTP through an httpx-compatible client
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..channels.hub import ChannelHub


@dataclass(frozen=True)
class ProviderSpec:
    kind: str  # channel kind it carries: "whatsapp", "voice"
    name: str  # value of WHATSAPP_PROVIDER / VOICE_PROVIDER
    build: Callable[["ChannelHub"], Any]
    summary: str


def _twilio_whatsapp(hub):
    from .twilio.whatsapp import TwilioWhatsApp
    return TwilioWhatsApp(hub)


def _meta_whatsapp(hub):
    from .meta.whatsapp import MetaWhatsApp
    return MetaWhatsApp(hub)


def _twilio_voice(hub):
    from .twilio.voice import TwilioVoice
    return TwilioVoice(hub)


def _vonage_voice(hub):
    from .vonage.voice import VonageVoice
    return VonageVoice(hub)


REGISTRY: dict[str, dict[str, ProviderSpec]] = {}


def register(spec: ProviderSpec) -> None:
    REGISTRY.setdefault(spec.kind, {})[spec.name] = spec


register(ProviderSpec("whatsapp", "twilio", _twilio_whatsapp, "WhatsApp through Twilio (TwiML webhooks + REST API)"))
register(ProviderSpec("whatsapp", "meta", _meta_whatsapp, "WhatsApp Cloud API, directly (Graph API)"))
register(ProviderSpec("voice", "twilio", _twilio_voice, "Twilio Programmable Voice (TwiML <Gather>)"))
register(ProviderSpec("voice", "vonage", _vonage_voice, "Vonage Voice API (NCCO talk/input)"))


def providers(kind: str) -> list[str]:
    """Registered provider names for a channel kind (drives the contract test matrix)."""
    return sorted(REGISTRY.get(kind, {}))
