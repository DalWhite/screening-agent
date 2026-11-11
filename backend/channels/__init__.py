"""Channels: the conversation logic of each way a candidate can reach us, vendor-neutral.

* `WebChannel` (web.py): the web chat (and API integrations): sessions by id, polling.
* `MessagingChannel` (messaging.py): WhatsApp: sessions by phone number, voice notes,
  media, service window and templates, delivery registry and ordering.
* `VoiceChannel` (voice.py): phone calls as a loop of neutral events returning `VoiceAction`s.
* `BaseChannel` (base.py): session lifecycle, channel continuity, transcript persistence.
* contracts.py: what integrations hand channels and must implement.
* `ChannelHub` (hub.py): which channels run, on which vendor integrations, outbound routing.

Vendor code (Twilio, Meta, Vonage, speech-to-text) lives in ``backend/integrations/``.
See docs/channel_architecture.md.
"""

from .hub import ChannelHub
from .messaging import MessagingChannel
from .voice import VoiceAction, VoiceChannel
from .web import WebChannel

__all__ = ["ChannelHub", "MessagingChannel", "VoiceAction", "VoiceChannel", "WebChannel"]
