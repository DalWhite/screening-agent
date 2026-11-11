"""Twilio Programmable Voice: the call is a TwiML loop around the provider-neutral `VoiceChannel`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import Gather, VoiceResponse

from ...channels.voice import VoiceAction, VoiceChannel
from .webhooks import VOICE_COLLECT, VOICE_INBOUND, VOICE_STATUS, root_router, shared_client, twilio_form, xml

if TYPE_CHECKING:
    from ...channels.hub import ChannelHub


class TwilioVoice:
    """Twilio Programmable Voice: the call is a TwiML loop.

    ``<Say>`` the agent's words inside a ``<Gather input="speech">`` whose action
    is the collect webhook; when the caller says nothing Twilio falls through to
    the ``<Redirect>`` (an empty result = silence); ``<Hangup>`` ends the call.
    """

    name = "twilio"
    kind = "voice"

    def __init__(self, hub: "ChannelHub", channel: Optional[VoiceChannel] = None):
        self.config = hub.config
        self.client = shared_client(hub)
        self.channel = channel or VoiceChannel(hub.agent, hub.store, profile_id=hub.config.voice_profile or None)

    @property
    def configured(self) -> bool:
        return self.client.configured

    @property
    def agent(self):
        return self.channel.agent

    @property
    def calls(self) -> dict[str, str]:
        return self.channel.calls

    def use_http(self, client: Any) -> None:
        self.client.use_http(client)

    # ----- events -> TwiML -----

    def handle_call_start(self, from_addr: str, call_sid: str) -> str:
        return self.render(self.channel.start_call(from_addr, call_sid))

    def handle_speech(self, session_id: str, speech: str, retries: int = 0, confidence: Optional[str] = None,
                      call_sid: Optional[str] = None) -> str:
        return self.render(self.channel.speech(session_id, speech, retries, confidence, call_sid))

    def handle_call_ended(self, call_sid: str, call_status: str) -> None:
        self.channel.call_ended(call_sid, call_status)

    def render(self, action: VoiceAction) -> str:
        response = VoiceResponse()
        say_kwargs: dict[str, Any] = {"language": action.locale}
        if self.config.voice_tts_voice:
            say_kwargs["voice"] = self.config.voice_tts_voice
        if action.hangup or not action.session_id:
            response.say(action.speech, **say_kwargs)
            response.hangup()
            return str(response)
        collect = (f"{self.config.public_base_url}{VOICE_COLLECT}"  # empty base: relative, resolved by Twilio
                   f"?session_id={quote(action.session_id)}&retries={action.retries}")
        gather = Gather(input="speech", language=action.locale, action=collect, method="POST", speech_timeout="auto")
        gather.say(action.speech, **say_kwargs)
        response.append(gather)
        response.redirect(collect, method="POST")  # no speech: re-enter collect with an empty result
        return str(response)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(VOICE_INBOUND)
        async def voice_inbound(request: Request) -> Response:
            """Incoming call: greet and start gathering speech."""
            form = await twilio_form(request, self.client, self.config.public_base_url)
            return xml(self.handle_call_start(form.get("From", ""), form.get("CallSid", "")))

        @router.post(VOICE_COLLECT)
        async def voice_collect(request: Request, session_id: str, retries: int = 0) -> Response:
            """Gather action: the caller's transcribed speech for one turn (empty = silence)."""
            form = await twilio_form(request, self.client, self.config.public_base_url)
            return xml(self.handle_speech(session_id, form.get("SpeechResult", ""), retries=retries,
                                           confidence=form.get("Confidence"), call_sid=form.get("CallSid")))

        @router.post(VOICE_STATUS)
        async def voice_status(request: Request) -> Response:
            """Status callback: record how the call ended."""
            form = await twilio_form(request, self.client, self.config.public_base_url)
            self.handle_call_ended(form.get("CallSid", ""), form.get("CallStatus", ""))
            return Response(status_code=204)

        return router

    def extra_routers(self) -> dict[str, Any]:
        return {"twilio-root": root_router}
