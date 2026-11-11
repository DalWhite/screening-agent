"""The contracts between channels and integrations.

Channels (conversation logic) never see a vendor's wire format: integrations
turn webhooks into these neutral events (`InboundMessage`, `StatusUpdate`) and
implement these interfaces (`MessagingClient` to send, `Transcriber` for voice
notes). Voice uses `VoiceAction` (channels/voice.py) in the other direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol


@dataclass
class MediaRef:
    """An attachment as the provider announces it. We only ever download voice notes."""

    mime_type: str = ""
    url: Optional[str] = None  # Twilio: MediaUrlN
    id: Optional[str] = None  # Meta: media id (resolved through the Graph API)
    voice: bool = False  # Meta marks push-to-talk recordings

    @property
    def is_audio(self) -> bool:
        return self.mime_type.split(";")[0].strip().lower().startswith("audio/")

    @property
    def media_type(self) -> str:
        """image / video / audio / document / sticker / ..."""
        main = self.mime_type.split(";")[0].strip().lower()
        if main == "image/webp":
            return "sticker"
        kind = main.split("/")[0] if "/" in main else ""
        return kind if kind in ("image", "video", "audio") else "document"


@dataclass
class InboundMessage:
    """One candidate message, whatever provider delivered it."""

    contact: str
    text: str = ""
    media: list[MediaRef] = field(default_factory=list)
    message_id: Optional[str] = None  # provider id, used to drop webhook retries
    profile_name: Optional[str] = None
    provider: str = ""
    kind: Optional[str] = None  # provider-specific type when not text/media (location, reaction...)

    @property
    def audio(self) -> Optional[MediaRef]:
        return next((m for m in self.media if m.is_audio), None)


@dataclass
class StatusUpdate:
    """Delivery receipt for a message we sent (Twilio status callback / Meta `statuses`)."""

    provider: str
    external_id: str
    status: str  # queued | sent | delivered | read | failed | undelivered
    error_code: Optional[int] = None
    error_detail: Optional[str] = None
    contact: Optional[str] = None


@dataclass
class SendResult:
    ok: bool
    id: Optional[str] = None
    error_code: Optional[int] = None
    error: Optional[str] = None


class MediaError(RuntimeError):
    """An attachment could not be downloaded."""


class MessagingClient(Protocol):
    """What a messaging provider's API client (Twilio, Meta Cloud API, ...) gives the channel.

    Inbound parsing and webhook routes live in the provider's adapter
    (``backend/integrations/<vendor>/``); this is the sending side plus the
    capabilities the channel needs to know about.
    """

    name: str  # "twilio" | "meta" | ...
    kind: str  # the channel it carries: "whatsapp" (later "sms", "telegram"...)
    replies_in_webhook: bool  # can the webhook answer carry the replies (Twilio TwiML)? Else: API calls
    window_error_codes: frozenset[int]  # "free-form refused: use a template" (empty: no service window)
    template_setting: str  # the setting that names the template (for error messages)

    @property
    def configured(self) -> bool: ...

    def render_webhook_reply(self, replies: list[str]) -> str: ...

    def send_text(self, to: str, body: str) -> SendResult: ...

    def template_configured(self) -> bool: ...

    @property
    def template_ref(self) -> str: ...

    def send_template(self, to: str, variables: Mapping[str, str], language: str) -> SendResult: ...

    def fetch_media(self, media: MediaRef, max_bytes: int) -> tuple[bytes, str]: ...

    def mark_read(self, message_id: str) -> None: ...

    def use_http(self, client) -> None:
        """Test seam: send every outbound HTTP request through ``client`` (an httpx-compatible client)."""



# ---------- speech-to-text (voice notes) ----------

class TranscriptionError(RuntimeError):
    pass


@dataclass
class Transcription:
    text: str
    language: Optional[str] = None
    duration: Optional[float] = None


class Transcriber:
    """Speech-to-text interface. This base is the "not configured" one: voice notes are
    answered with "please type it". Implementations: ``integrations/speech/``."""

    name = "none"
    model = ""

    @property
    def available(self) -> bool:
        return False

    def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        raise TranscriptionError("speech-to-text is not configured (STT_PROVIDER)")

    def use_http(self, client) -> None:
        """Test seam (no HTTP when speech-to-text is off)."""
