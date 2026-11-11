"""Speech in the simulator.

* **Voice notes** (WhatsApp): a simulated voice note is a real, playable WAV
  (a soft tone as long as the sentence would take to say) carrying what the
  candidate "said" in a RIFF ``LIST/INFO/ICMT`` chunk.
* **The speech-to-text service** the app sends voice notes to
  (OpenAI-compatible ``/v1/audio/transcriptions``): it reads that chunk back,
  so voice notes go through the app's real download + transcription code
  without any audio model. Real recordings have no such chunk: it returns an
  empty transcript (what a failed transcription looks like to the app).
* **Phone speech recognition**: how a voice provider writes what a caller
  says (``ChannelSimulator(transcriber=...)``). Twilio posts ``SpeechResult``
  as a sentence: capitalised, with final punctuation, numbers as digits.
  Plainer engines lowercase everything and drop punctuation; spelled-out
  numbers come back digit by digit. Compose presets with :func:`chain`.
"""

from __future__ import annotations

import io
import json
import math
import re
import struct
import time
import wave
from typing import TYPE_CHECKING, Any, Callable, Optional

from .core import LogEntry

if TYPE_CHECKING:
    from .core import ChannelSimulator

# ---------- voice notes ----------

SAMPLE_RATE = 8000
SECONDS_PER_WORD = 0.35


def make_voice_note(text: str, language: str = "es") -> bytes:
    words = max(len(text.split()), 1)
    frames = int(SAMPLE_RATE * min(max(words * SECONDS_PER_WORD, 1.0), 60.0))
    pcm = io.BytesIO()
    with wave.open(pcm, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(
            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE) * (0.6 + 0.4 * math.sin(i / 900))))
            for i in range(frames)))
    riff = pcm.getvalue()
    comment = json.dumps({"transcript": text, "language": language}, ensure_ascii=False).encode("utf-8")
    if len(comment) % 2:
        comment += b"\0"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment
    chunk = b"LIST" + struct.pack("<I", len(info)) + info
    body = riff[8:] + chunk  # "WAVE" + fmt + data + LIST
    return b"RIFF" + struct.pack("<I", len(body)) + body


def read_voice_note(data: bytes) -> Optional[dict]:
    """``{"transcript", "language", "duration"}`` of a simulated voice note, None for real audio."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, found, duration = 12, None, None
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        payload = data[pos + 8:pos + 8 + size]
        if tag == b"data":
            duration = round(size / (SAMPLE_RATE * 2), 2)
        elif tag == b"LIST" and payload[:4] == b"INFO" and payload[4:8] == b"ICMT":
            length = struct.unpack("<I", payload[8:12])[0]
            try:
                found = json.loads(payload[12:12 + length].rstrip(b"\0").decode("utf-8"))
            except ValueError:
                return None
        pos += 8 + size + (size % 2)
    if found is None:
        return None
    return {**found, "duration": duration}


# ---------- the speech-to-text service ----------

class SpeechSim:
    """The speech-to-text service the app transcribes voice notes with."""

    def __init__(self, sim: "ChannelSimulator", api_key: str = "sim-stt-key"):
        self.sim = sim
        self.api_key = api_key

    def transcribe(self, data: bytes, authorization: str) -> tuple[int, dict[str, Any]]:
        """OpenAI-compatible ``/v1/audio/transcriptions``: understands simulated voice notes."""
        entry = LogEntry(kind="rest", method="POST", url="/v1/audio/transcriptions", params={"bytes": len(data)})
        started = time.monotonic()
        if self.sim.faults.stt_down:
            status, body = 503, {"error": {"message": "speech-to-text unavailable", "type": "server_error"}}
        elif self.api_key and authorization != f"Bearer {self.api_key}":
            status, body = 401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}
        else:
            note = read_voice_note(data)
            status = 200
            body = ({"text": note["transcript"], "language": {"es": "spanish", "en": "english"}.get(
                        note.get("language"), note.get("language")), "duration": note.get("duration")}
                    if note else {"text": ""})
        entry.status, entry.response = status, json.dumps(body, ensure_ascii=False)
        if status >= 400:
            entry.error = body["error"]["message"]
        self.sim._log(entry, started, None)
        return status, body


# ---------- phone speech recognition (what a voice provider posts for the caller's words) ----------

Transcriber = Callable[[str], str]


def twilio_style(text: str) -> str:
    """'sí, tengo 34 años' -> 'Sí, tengo 34 años.' (what Twilio's <Gather input="speech"> posts)."""
    text = re.sub(r"[¿¡]", "", text).strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".?!" else f"{text}."


def plain_lowercase(text: str) -> str:
    """'Sí, tengo 34 años.' -> 'sí tengo 34 años' (no casing, no punctuation)."""
    return re.sub(r"\s+", " ", re.sub(r"[¿?¡!.,;:]", "", text)).lower().strip()


def digit_by_digit(text: str) -> str:
    """'12345678Z' -> '1 2 3 4 5 6 7 8 Z': an ID number spelled out on the phone."""
    return re.sub(r"\b([A-Za-z]?)(\d{6,})([A-Za-z]?)\b",
                  lambda m: " ".join(filter(None, [m.group(1), " ".join(m.group(2)), m.group(3)])), text)


def chain(*steps: Transcriber) -> Transcriber:
    def run(text: str) -> str:
        for step in steps:
            text = step(text)
        return text
    return run


DEFAULT_STT_STYLE = twilio_style
