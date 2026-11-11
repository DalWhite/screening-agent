"""Speech-to-text client for WhatsApp voice notes (implements `channels.contracts.Transcriber`).

Any OpenAI-compatible ``/audio/transcriptions`` endpoint works: OpenAI
Whisper, Groq, a local faster-whisper / whisper.cpp server, or the local
simulator (``python -m simulators``). The audio is sent for transcription and
then dropped; only the transcript is stored, and it goes through the same
guardrails and PII redaction as typed text.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ...channels.contracts import Transcriber, Transcription, TranscriptionError
from ...config import settings

log = logging.getLogger("sazon.stt")

_EXTENSIONS = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
    "audio/aac": "m4a", "audio/x-m4a": "m4a", "audio/amr": "amr", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/webm": "webm", "audio/flac": "flac",
}


class OpenAICompatibleTranscriber(Transcriber):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0,
                 http: Optional[Any] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._http = http  # httpx.Client-compatible; tests plug the simulator in here

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    def use_http(self, client: Any) -> None:
        """Test seam: transcription requests go through ``client``."""
        self._http = client

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        main = mime_type.split(";")[0].strip().lower()
        filename = f"voice-note.{_EXTENSIONS.get(main, 'ogg')}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body: dict[str, Any] = {}
        for response_format in ("verbose_json", "json"):  # not every server speaks verbose_json
            try:
                resp = self.http.post(f"{self.base_url}/audio/transcriptions", headers=headers,
                                      files={"file": (filename, audio, main or "application/octet-stream")},
                                      data={"model": self.model, "response_format": response_format})
            except httpx.HTTPError as exc:
                raise TranscriptionError(f"speech-to-text unreachable: {exc}") from exc
            if resp.status_code in (400, 422) and response_format == "verbose_json":
                continue
            if resp.status_code >= 400:
                raise TranscriptionError(f"speech-to-text answered {resp.status_code}: {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise TranscriptionError("speech-to-text answered something that is not JSON") from exc
            break
        text = str(body.get("text") or "").strip()
        if not text:
            raise TranscriptionError("empty transcript")
        duration = body.get("duration")
        return Transcription(text=text, language=body.get("language"),
                             duration=float(duration) if isinstance(duration, (int, float)) else None)


def build_transcriber(config: Any = settings) -> Transcriber:
    provider = (config.stt_provider or "auto").lower()
    if provider == "none" or (provider == "auto" and not config.stt_base_url):
        return Transcriber()
    if provider not in ("auto", "openai"):
        log.warning("unknown STT_PROVIDER=%r: voice notes disabled", provider)
        return Transcriber()
    return OpenAICompatibleTranscriber(config.stt_base_url or config.llm_base_url, config.stt_api_key,
                                       config.stt_model, timeout=config.stt_timeout)
