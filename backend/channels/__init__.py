"""Conversation channels beyond the web chat.

The screening agent itself is channel-agnostic: it takes text in and produces
text out. Each channel adapter here maps a transport (Twilio WhatsApp webhooks,
Twilio Voice TwiML callbacks) onto the same `ScreeningAgent` + `SessionStore`
pair the web chat uses, so validation, guardrails, re-engagement and analytics
behave identically regardless of where the candidate talks to us.
"""

from .twilio_provider import TwilioProvider
from .voice import VoiceChannel
from .whatsapp import WhatsAppChannel

__all__ = ["TwilioProvider", "VoiceChannel", "WhatsAppChannel"]
