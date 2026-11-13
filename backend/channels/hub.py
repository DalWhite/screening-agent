"""The channel hub: the composition root of the channels.

Which channels run, on which providers (vendor integrations), and how to reach a
candidate. The only module of ``channels/`` that knows integrations exist.

Built from configuration (``WHATSAPP_PROVIDER``, ``VOICE_PROVIDER``; ``none`` turns
a channel off) through the provider registry. The app mounts the hub's routers and
hands its notifiers to the parts that push (re-engagement, backoffice, external
checks); none of them knows which channels or providers exist.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..session_store import SessionStore
from ..integrations.registry import REGISTRY
from ..integrations.speech.client import build_transcriber
from .contracts import Transcriber
from .voice import VoiceChannel
from .web import WebChannel
from .messaging import MessagingChannel

log = logging.getLogger("sazon.channels")

OFF = {"", "none", "off", "disabled"}

# Labels the backoffice shows; new channel kinds add theirs here.
KIND_LABELS = {"web": {"es": "Chat web", "en": "Web chat"}, "whatsapp": {"es": "WhatsApp", "en": "WhatsApp"},
               "voice": {"es": "Teléfono", "en": "Phone"}}


class ChannelHub:
    def __init__(self, agent: ScreeningAgent, store: SessionStore, config: Any = settings,
                 transcriber: Optional[Transcriber] = None, selection: Optional[dict[str, str]] = None):
        self.agent = agent
        self.store = store
        self.config = config
        self.transcriber = transcriber or build_transcriber(config)
        self._shared: dict[str, Any] = {}
        self.web = WebChannel(agent, store)  # always on: its transport is our own API (backend/api/sessions.py)
        self.adapters: dict[str, Any] = {}  # channel kind -> vendor adapter (integrations/<vendor>/)
        chosen = selection or {"whatsapp": config.whatsapp_provider, "voice": config.voice_provider}
        for kind, name in chosen.items():
            name = (name or "").strip().lower()
            if name in OFF:
                continue
            spec = REGISTRY.get(kind, {}).get(name)
            if spec is None:
                raise ValueError(f"unknown {kind} provider {name!r}; available: {sorted(REGISTRY.get(kind, {}))}")
            self.adapters[kind] = spec.build(self)

    # ----- building blocks for adapters -----

    def shared(self, key: str, factory: Callable[[], Any]) -> Any:
        """One instance per hub (e.g. a Twilio client used by both WhatsApp and voice)."""
        if key not in self._shared:
            self._shared[key] = factory()
        return self._shared[key]

    def adapter(self, kind: str) -> Optional[Any]:
        return self.adapters.get(kind)

    # ----- channels -----

    @property
    def messaging(self) -> dict[str, MessagingChannel]:
        """Push-capable channels by name (the channels we can write to first)."""
        return {kind: a.channel for kind, a in self.adapters.items() if isinstance(a.channel, MessagingChannel)}

    @property
    def voice(self) -> Optional[VoiceChannel]:
        adapter = self.adapters.get("voice")
        return adapter.channel if adapter is not None else None

    def routers(self) -> list[APIRouter]:
        routers = [a.router() for a in self.adapters.values()]
        shared: dict[str, Callable[["ChannelHub"], APIRouter]] = {}
        for adapter in self.adapters.values():
            shared.update(getattr(adapter, "extra_routers", dict)())  # optional
        return routers + [factory(self) for factory in shared.values()]

    def use_http(self, client: Any) -> None:
        """Test seam: every provider's and the STT's outbound HTTP through ``client``."""
        for adapter in self.adapters.values():
            adapter.use_http(client)
        self.transcriber.use_http(client)

    # ----- reaching a candidate -----

    def _push_channels(self, state: SessionState) -> list[MessagingChannel]:
        channels = self.messaging
        order = [state.current_channel] + list(reversed(state.channels or [state.source])) + list(channels)
        seen: list[MessagingChannel] = []
        for name in order:
            channel = channels.get(name)
            if channel is not None and channel not in seen and channel.can_reach(state):
                seen.append(channel)
        return seen

    def notify(self, state: SessionState, text: str) -> Optional[str]:
        """Recruiter message / check decision. Returns the channel that delivered it, or None (queued)."""
        for channel in self._push_channels(state):
            if channel.send_message(state, text):
                return channel.source
        return None

    def nudge(self, state: SessionState, text: str) -> Optional[str]:
        """Re-engagement nudge (a template may stand in for it). Never blocks: runs on the event loop."""
        for channel in self._push_channels(state):
            if channel.send_outbound(state, text, purpose="nudge"):
                return channel.source
        return None

    def describe(self) -> dict[str, dict[str, Any]]:
        """For the backoffice: active channels, their provider and whether we can write to them first
        (the web chat is reached when the candidate polls, not pushed)."""
        channels = {"web": {"provider": "builtin", "label": KIND_LABELS["web"], "push": False, "configured": True}}
        channels.update({kind: {"provider": a.name, "label": KIND_LABELS.get(kind, {"es": kind, "en": kind}),
                                "push": isinstance(a.channel, MessagingChannel), "configured": a.configured}
                         for kind, a in self.adapters.items()})
        return channels
