"""Local stand-ins for the channel providers (docs/simulators.md).

Layout (mirrors ``backend/integrations/``):

* ``core.py``     `ChannelSimulator`: phones, calls (caller's side), media, log, delivery machinery
* ``twilio.py``   Twilio WhatsApp (TwiML + REST API) and Twilio Voice (TwiML call executor)
* ``meta.py``     Meta WhatsApp Cloud API (webhooks + Graph API)
* ``vonage.py``   Vonage Voice (NCCO call executor, signed webhooks)
* ``speech.py``   voice-note audio, the speech-to-text service, phone STT presets
* ``twiml.py``    TwiML parser
* ``server.py``   HTTP face: the providers' APIs, control API, phone UI (``static/``)
* ``runner.py``   processes: simulator server, the app wired to it
* ``testing.py``  pytest harness (in-process and live stacks, transcript parity)
* ``__main__.py`` ``python -m simulators --with-app``
"""

from .core import ChannelSimulator

__all__ = ["ChannelSimulator"]
