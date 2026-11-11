"""Vendor integrations: everything that talks to an external service.

One package per vendor, each with its API client (``client.py``: what we call)
and its channel adapters (``whatsapp.py``, ``voice.py``: the webhooks it calls
us with, in its wire format):

* ``twilio/``  WhatsApp + voice (TwiML)
* ``meta/``    WhatsApp Cloud API
* ``vonage/``  voice (NCCO)
* ``speech/``  speech-to-text for voice notes (OpenAI-compatible)
* ``registry.py`` which vendor serves which channel (``WHATSAPP_PROVIDER``, ``VOICE_PROVIDER``)

Channels (``backend/channels/``) never import from here; the channel hub does.
"""
