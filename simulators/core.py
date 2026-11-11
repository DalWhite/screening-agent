"""The channel simulator's core: phones, calls, media, the log and the delivery machinery.

Vendor-neutral on purpose, like ``backend/channels/``: everything in a vendor's
wire format lives in its plugin (``twilio.py``, ``meta.py``, ``vonage.py``;
speech in ``speech.py``). `ChannelSimulator` composes them and delegates
WhatsApp to the configured messaging plugin and calls to the configured voice
plugin, so a candidate's `Phone` and `BaseCall` look the same whatever the
providers:

    sim = ChannelSimulator(app_url, whatsapp_provider="meta", voice_provider="vonage")
    phone = sim.phone("+34600111222")
    phone.whatsapp("hola"); phone.voice_note("sí"); call = phone.call(); call.say("Getafe")

Nothing here talks to the internet. The same object backs the pytest harness
(``testing.py``: in-process or against a live server) and the browser UI
(``server.py``).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import logging
import queue
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Union

import httpx

log = logging.getLogger("simulators")

DEFAULT_WHATSAPP_NUMBER = "+14155238886"  # the number of Twilio's WhatsApp Sandbox
DEFAULT_VOICE_NUMBER = "+34910000000"
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)

# Provider plugins. A new provider = a plugin module + an entry here (and in the backend registry).
MESSAGING_PROVIDERS = ("twilio", "meta")
VOICE_PROVIDERS = ("twilio", "vonage")

APPLICATION_ERROR_SPEECH = "We are sorry, an application error has occurred. Goodbye."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sid(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def e164(number: str) -> str:
    """``whatsapp:+34 600 111 222`` -> ``+34600111222``."""
    raw = (number or "").strip().removeprefix("whatsapp:").replace(" ", "")
    if not raw.startswith("+"):
        raw = "+" + raw
    if not re.fullmatch(r"\+\d{6,15}", raw):
        raise ValueError(f"not an E.164 phone number: {number!r}")
    return raw


# ---------- configuration ----------

@dataclass
class ChannelPaths:
    """Where each provider's console points its webhooks (see docs/channels.md)."""

    whatsapp_inbound: str = "/api/channels/whatsapp/inbound"
    voice_inbound: str = "/api/channels/voice/inbound"
    voice_status: str = "/api/channels/voice/status"
    meta_webhook: str = "/api/channels/whatsapp/meta"


@dataclass
class Faults:
    """Failure injection: how providers misbehave in production."""

    rest_down: bool = False  # provider API outage: Twilio answers 503 (20503), Meta code 2
    template_only: bool = False  # Twilio sender only accepts Content Templates: free-form -> 21654
    enforce_window: bool = True  # WhatsApp 24h customer-service window for free-form messages
    # How an out-of-window free-form message fails on Twilio. "sync": Messages.create is refused with
    # 63016. "async": accepted as queued and later failed with 63016 without reaching the phone.
    # (Meta always reports 131047 asynchronously.)
    window_errors: str = "sync"
    webhook_timeout: float = 15.0  # Twilio abandons webhooks after 15s (error 11200 / 11205)
    stt_down: bool = False  # speech-to-text endpoint answers 503 (voice notes cannot be transcribed)
    # Meta development mode: only these numbers may receive messages (error 131030). Empty = anyone.
    recipient_allow_list: list[str] = field(default_factory=list)
    # Delivery receipts reach the app before the API call that sent the message has returned
    # (providers do not guarantee the order): the app must not lose a failure it hears about "early".
    receipts_before_response: bool = False


# ---------- records ----------

_seq = itertools.count(1)


@dataclass
class WhatsAppMessage:
    sender: str  # "candidate" | "business"
    body: str
    # candidate: "webhook"; business: how it arrived, in the provider's terms: "twiml" (in the webhook
    # answer), "rest" (Twilio API), "graph" (Meta Graph API), "template" (approved template)
    via: str
    sid: str = field(default_factory=lambda: _sid("SM"))
    at: datetime = field(default_factory=_now)
    status: str = "delivered"  # business messages: delivered | failed (never reached the phone)
    error_code: Optional[int] = None
    content_sid: Optional[str] = None  # Twilio Content SID or Meta template name
    media: list[str] = field(default_factory=list)  # candidate: media URLs announced to the app
    provider: str = "twilio"
    in_reply: bool = False  # business: sent while the app was still answering the candidate's webhook
    media_id: Optional[str] = None  # candidate: attachment in the simulator's media store
    media_mime: Optional[str] = None
    transcript: Optional[str] = None  # candidate voice note: what it says
    read: bool = False  # candidate: the business marked it read / business: the candidate read it
    seq: int = field(default_factory=lambda: next(_seq))

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"

    @property
    def pushed(self) -> bool:
        """Business message that did not answer a webhook in flight (nudge, recruiter message, late reply)."""
        return self.sender == "business" and not self.in_reply

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat(timespec="milliseconds")
        return data


@dataclass
class Utterance:
    speaker: str  # "agent" | "caller" | "system"
    text: str
    language: Optional[str] = None
    at: datetime = field(default_factory=_now)
    seq: int = field(default_factory=lambda: next(_seq))

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat(timespec="milliseconds")
        return data


@dataclass
class LogEntry:
    kind: str  # webhook | rest | status_callback | note
    method: str
    url: str
    params: dict[str, Any]
    status: Optional[int] = None
    response: str = ""
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None
    phone: Optional[str] = None
    at: datetime = field(default_factory=_now)
    seq: int = field(default_factory=lambda: next(_seq))

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat(timespec="milliseconds")
        return data


@dataclass
class Media:
    """An attachment a candidate sent (voice note, photo), served to the app like the provider does."""

    data: bytes
    mime: str
    transcript: Optional[str] = None
    voice: bool = False
    id: str = field(default_factory=lambda: str(10**15 + secrets.randbelow(9 * 10**15)))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def whatsapp_type(self) -> str:
        main = self.mime.split(";")[0].strip()
        if main == "image/webp":
            return "sticker"
        kind = main.split("/")[0]
        return kind if kind in ("audio", "image", "video") else "document"


# 1x1 JPEG: what a simulated photo contains (the app must never download or keep it anyway)
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5"
    "PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


class SimulationError(RuntimeError):
    """The scenario did something a real phone could not (e.g. speak on a finished call)."""


# ---------- voice call (the caller's side; plugins execute their call-control language) ----------

class BaseCall:
    """One inbound phone call, whatever the voice provider.

    The caller-side API (``say``, ``silence``, ``hangup``, ``prompt``,
    ``gather_language``...) is the same for every provider; subclasses execute
    their provider's call-control language (Twilio: TwiML, Vonage: NCCO) and
    talk to the app in its webhook format.
    """

    provider = ""
    id_prefix = "CA"

    def __init__(self, sim: "ChannelSimulator", phone: "Phone"):
        self.sim = sim
        self.phone = phone
        self.sid = _sid(self.id_prefix)
        self.state = "ringing"  # ringing | in-progress | completed
        self.end_reason: Optional[str] = None  # hangup-by-app | caller-hangup | end-of-twiml | application-error
        self.transcript: list[Utterance] = []
        self.started_at = _now()
        self.ended_at: Optional[datetime] = None
        self._heard_since_input: list[tuple[str, Optional[str]]] = []  # (text, locale) since the caller spoke
        self._lock = threading.RLock()

    # ----- provider hooks -----

    def _start(self) -> None:
        raise NotImplementedError

    def _listening_language(self) -> Optional[str]:
        """STT locale while the provider listens for the caller, None when it does not listen."""
        raise NotImplementedError

    def _send_speech(self, speech: str, confidence: float) -> None:
        raise NotImplementedError

    def _send_silence(self) -> None:
        raise NotImplementedError

    def _stop_listening(self) -> None:
        raise NotImplementedError

    def _status_callback(self) -> None:
        """Tell the app the call is over (if the provider does)."""

    # ----- state -----

    @property
    def waiting(self) -> bool:
        """True while the provider is listening for the caller."""
        return self.state == "in-progress" and self._listening_language() is not None

    @property
    def active(self) -> bool:
        return self.state != "completed"

    @property
    def gather_language(self) -> Optional[str]:
        """STT locale the provider listens in (follows the candidate's language)."""
        return self._listening_language() if self.state != "completed" else None

    @property
    def prompt(self) -> str:
        """Everything the agent said since the caller last spoke."""
        return " ".join(text for text, _ in self._heard_since_input).strip()

    @property
    def prompt_language(self) -> Optional[str]:
        """TTS locale of the last thing the caller heard."""
        langs = [lang for _, lang in self._heard_since_input if lang]
        return langs[-1] if langs else None

    def agent_lines(self) -> list[str]:
        return [u.text for u in self.transcript if u.speaker == "agent"]

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid, "provider": self.provider, "phone": self.phone.number, "state": self.state,
            "end_reason": self.end_reason, "waiting": self.waiting, "gather_language": self.gather_language,
            "prompt": self.prompt, "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "ended_at": self.ended_at.isoformat(timespec="milliseconds") if self.ended_at else None,
            "transcript": [u.to_json() for u in self.transcript],
        }

    # ----- caller actions -----

    def say(self, text: str, confidence: float = 0.92) -> str:
        """The caller speaks; the provider's STT sends the transcript to the app.
        Returns what the agent answered (the new :attr:`prompt`)."""
        with self._lock:
            self._require_waiting("speak")
            speech = self.sim.transcribe(text)
            self._hear("caller", speech)
            self._heard_since_input = []
            self._send_speech(speech, confidence)
            return self.prompt

    def silence(self) -> str:
        """The caller says nothing until the provider's listening times out."""
        with self._lock:
            self._require_waiting("stay silent")
            self._hear("system", "(silencio)")
            self._heard_since_input = []
            self._send_silence()
            return self.prompt

    def hangup(self) -> None:
        """The caller hangs up (mid-question or after the goodbye)."""
        with self._lock:
            if self.state == "completed":
                return
            self._hear("system", "(el candidato cuelga)")
            self._end("caller-hangup")

    # ----- shared helpers -----

    def _agent_says(self, text: str, language: Optional[str]) -> None:
        self._heard_since_input.append((text, language))
        self._hear("agent", text, language)

    def _application_error(self, reason: str) -> None:
        self.sim.note(f"call {self.sid}: application error ({reason})", phone=self.phone.number)
        self._hear("agent", APPLICATION_ERROR_SPEECH, "en-US")
        self._end("application-error")

    def _hear(self, speaker: str, text: str, language: Optional[str] = None) -> None:
        self.transcript.append(Utterance(speaker=speaker, text=text, language=language))
        self.sim._changed()

    def _require_waiting(self, action: str) -> None:
        if self.state == "completed":
            raise SimulationError(f"cannot {action}: call {self.sid} already ended ({self.end_reason})")
        if self._listening_language() is None:
            raise SimulationError(f"cannot {action}: the provider is not listening")

    def _end(self, reason: str) -> None:
        self._stop_listening()
        self.state, self.end_reason, self.ended_at = "completed", reason, _now()
        self.sim._changed()
        self._status_callback()


# ---------- phone ----------

class Phone:
    """A candidate's handset: one WhatsApp thread with the business + phone calls."""

    def __init__(self, sim: "ChannelSimulator", number: str, name: str = ""):
        self.sim = sim
        self.number = number
        self.name = name
        self.thread: list[WhatsAppMessage] = []
        self.calls: list[BaseCall] = []
        self.last_inbound_at: Optional[datetime] = None  # opens WhatsApp's 24h customer-service window
        self._exchanges = 0  # webhooks in flight for this phone (Meta replies arrive during them)
        self._last_inbound: Any = None  # the last inbound webhook, as the provider plugin sent it

    # ----- WhatsApp -----

    def whatsapp(self, body: str = "", media: Iterable[str] = (), attachment: Optional[Media] = None) -> list[str]:
        """Send a WhatsApp message to the business through the configured provider plugin.
        Returns the replies the candidate got while the app handled it (TwiML with Twilio, Graph
        API calls made during the webhook with Meta). Later pushes land in :attr:`thread`.
        ``media``: raw media URLs (Twilio only)."""
        media = list(media)
        message = WhatsAppMessage(sender="candidate", body=body, via="webhook", media=media,
                                  provider=self.sim.whatsapp_provider, sid=self.sim.messaging.new_message_id(),
                                  media_id=attachment.id if attachment else None,
                                  media_mime=attachment.mime if attachment else None,
                                  transcript=attachment.transcript if attachment else None)
        with self.sim._lock:
            self.thread.append(message)
            self.last_inbound_at = _now()
            self.sim._changed()
        self.sim.candidate_read_everything(self)
        return self.sim.messaging.inbound(self, message, attachment)

    def voice_note(self, text: str, language: str = "es", mime: str = "audio/ogg; codecs=opus") -> list[str]:
        """Record and send a voice note saying ``text`` (the simulator's STT will hear exactly that)."""
        from .speech import make_voice_note

        media = self.sim.store_media(make_voice_note(text, language), mime, transcript=text, voice=True)
        return self.whatsapp("", attachment=media)

    def audio(self, data: bytes, mime: str = "audio/ogg; codecs=opus") -> list[str]:
        """Send real audio (e.g. recorded in the UI): only a real speech-to-text service can read it."""
        return self.whatsapp("", attachment=self.sim.store_media(data, mime, voice=True))

    def photo(self, caption: str = "", mime: str = "image/jpeg") -> list[str]:
        return self.whatsapp(caption, attachment=self.sim.store_media(_TINY_JPEG, mime))

    def resend_last(self) -> list[str]:
        """Deliver the last inbound webhook again, same message id (what providers do on retries)."""
        if self._last_inbound is None:
            raise SimulationError("nothing to resend")
        return self.sim.messaging.resend(self, self._last_inbound)

    def _exchange(self, post: Callable[[], Any]) -> list[str]:
        """Run a webhook; messages the business sends meanwhile count as its replies."""
        with self.sim._lock:
            self._exchanges += 1
            mark = len(self.thread)
        try:
            post()
        finally:
            with self.sim._lock:
                self._exchanges -= 1
        with self.sim._lock:
            return [m.body for m in self.thread[mark:] if m.sender == "business" and m.in_reply and m.delivered]

    def received(self, since: int = 0, delivered_only: bool = True) -> list[WhatsAppMessage]:
        """Business messages in the thread (after ``since`` = a previous ``len(thread)``)."""
        with self.sim._lock:
            return [m for m in self.thread[since:]
                    if m.sender == "business" and (m.delivered or not delivered_only)]

    def texts(self, since: int = 0) -> list[str]:
        return [m.body for m in self.received(since)]

    def wait_for_whatsapp(self, match: Union[str, Callable[[WhatsAppMessage], bool], None] = None,
                          since: int = 0, timeout: float = 10.0) -> WhatsAppMessage:
        """Block until a delivered business message (optionally containing ``match``) arrives."""
        predicate = match if callable(match) else (lambda m: match is None or match.lower() in m.body.lower())
        deadline = time.monotonic() + timeout
        with self.sim._cond:
            while True:
                for message in self.received(since):
                    if predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    got = [m.body[:80] for m in self.received(since)]
                    raise AssertionError(f"no WhatsApp message matching {match!r} for {self.number} "
                                         f"after {timeout}s; received: {got}")
                self.sim._cond.wait(remaining)

    def window_open(self, now: Optional[datetime] = None) -> bool:
        return self.last_inbound_at is not None and (now or _now()) - self.last_inbound_at < CUSTOMER_SERVICE_WINDOW

    def close_window(self) -> None:
        """Pretend the candidate's last message was more than 24h ago."""
        with self.sim._lock:
            self.last_inbound_at = _now() - CUSTOMER_SERVICE_WINDOW - timedelta(minutes=1)
            self.sim._changed()

    # ----- voice -----

    def call(self) -> BaseCall:
        """Dial the business number. Returns once the provider waits for the caller (or the call ended)."""
        call = self.sim.new_call(self)
        with self.sim._lock:
            self.calls.append(call)
            self.sim._changed()
        call._start()
        return call

    @property
    def active_call(self) -> Optional[BaseCall]:
        return next((c for c in reversed(self.calls) if c.active), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number, "name": self.name, "window_open": self.window_open(),
            "thread": [m.to_json() for m in self.thread], "calls": [c.to_json() for c in self.calls],
        }


# ---------- the simulator ----------

Transport = Any  # anything with httpx.Client's .request(): httpx.Client, fastapi.testclient.TestClient


class ChannelSimulator:
    """The providers a screening deployment talks to, simulated: one WhatsApp sender, one voice number.

    :param app_url: where the screening app listens (``http://127.0.0.1:8000``,
        or ``http://testserver`` with FastAPI's TestClient).
    :param transport: HTTP client used for webhooks (defaults to ``httpx.Client``).
    :param whatsapp_provider: messaging plugin (``MESSAGING_PROVIDERS``).
    :param voice_provider: voice plugin (``VOICE_PROVIDERS``).
    :param public_url: the URL providers *believe* the app has (``PUBLIC_BASE_URL``,
        e.g. an ngrok domain). Webhooks are signed for it and absolute URLs
        pointing at it are routed to ``app_url``. Defaults to ``app_url``.
    :param graph_url: where the app reaches this simulator's own HTTP API
        (Graph API media URLs, speech-to-text).
    :param sign: sign webhooks (each plugin its way).
    :param transcriber: how phone speech recognition writes what the caller says
        (``speech.py``; default: Twilio-style sentences).
    :param twilio / meta / vonage: plugin settings (credentials, secrets...).
    """

    def __init__(self, app_url: str, transport: Optional[Transport] = None, *,
                 whatsapp_provider: str = "twilio", voice_provider: str = "twilio",
                 whatsapp_number: str = DEFAULT_WHATSAPP_NUMBER, voice_number: str = DEFAULT_VOICE_NUMBER,
                 public_url: Optional[str] = None, graph_url: str = "", paths: Optional[ChannelPaths] = None,
                 sign: bool = True, send_status_callbacks: bool = True,
                 transcriber: Optional[Callable[[str], str]] = None, stt_api_key: str = "sim-stt-key",
                 twilio: Optional[dict[str, Any]] = None, meta: Optional[dict[str, Any]] = None,
                 vonage: Optional[dict[str, Any]] = None):
        if whatsapp_provider not in MESSAGING_PROVIDERS:
            raise ValueError(f"whatsapp_provider must be one of {MESSAGING_PROVIDERS}")
        if voice_provider not in VOICE_PROVIDERS:
            raise ValueError(f"voice_provider must be one of {VOICE_PROVIDERS}")
        self.app_url = app_url.rstrip("/")
        self._public = (public_url or app_url).rstrip("/")
        self._owns_transport = transport is None
        self.transport = transport or httpx.Client(timeout=None)
        self.whatsapp_number = e164(whatsapp_number)
        self.voice_number = e164(voice_number)
        self.paths = paths or ChannelPaths()
        self.sign = sign
        self.send_status_callbacks = send_status_callbacks
        self.transcriber = transcriber
        self.graph_url = graph_url.rstrip("/")
        self.faults = Faults()
        self.phones: dict[str, Phone] = {}
        self.media: dict[str, Media] = {}
        self.log: deque[LogEntry] = deque(maxlen=400)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.version = 0  # bumps on every change (UI polling)
        self._jobs: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

        from .meta import MetaSim
        from .speech import SpeechSim
        from .twilio import TwilioSim
        from .vonage import VonageSim

        self.twilio = TwilioSim(self, **(twilio or {}))
        self.meta = MetaSim(self, sign=sign, **(meta or {}))
        self.vonage = VonageSim(self, sign=sign, **(vonage or {}))
        self.speech = SpeechSim(self, api_key=stt_api_key)
        self.whatsapp_provider = whatsapp_provider
        self.voice_provider = voice_provider

    @property
    def messaging(self):
        """The plugin WhatsApp runs on (``inbound``, ``resend``, ``receipt``, ``new_message_id``)."""
        return {"twilio": self.twilio, "meta": self.meta}[self.whatsapp_provider]

    @property
    def voice(self):
        """The plugin calls run on (``new_call``)."""
        return {"twilio": self.twilio, "vonage": self.vonage}[self.voice_provider]

    def plugin(self, provider: str):
        return {"twilio": self.twilio, "meta": self.meta, "vonage": self.vonage}[provider]

    # ----- lifecycle -----

    def close(self) -> None:
        if self._worker is not None:
            self.flush(timeout=5)
            self._jobs.put(None)
            self._worker.join(timeout=5)
            self._worker = None
        if self._owns_transport:
            self.transport.close()

    def reset(self) -> None:
        self.flush(timeout=5)
        with self._lock:
            self.phones.clear()
            self.log.clear()
            self.media.clear()
            self.twilio.rest_messages.clear()
            self.meta.sent.clear()
            self.faults = Faults()
            self._changed()

    # ----- asynchronous deliveries (status callbacks arrive after the API call returned) -----

    def dispatch(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="simulator-dispatch", daemon=True)
                self._worker.start()
        self._jobs.put(job)

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                job()
            except Exception:  # noqa: BLE001 - a failing callback must not kill the dispatcher
                log.exception("simulator job failed")
            finally:
                self._jobs.task_done()

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until every pending status callback / webhook has been delivered."""
        deadline = time.monotonic() + timeout
        while self._jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def _changed(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait_for_change(self, version: int, timeout: float) -> int:
        with self._cond:
            if self.version == version:
                self._cond.wait(timeout)
            return self.version

    # ----- phones and calls -----

    def phone(self, number: str, name: str = "") -> Phone:
        number = e164(number)
        with self._lock:
            phone = self.phones.get(number)
            if phone is None:
                phone = self.phones[number] = Phone(self, number, name)
                self._changed()
            elif name and not phone.name:
                phone.name = name
            return phone

    def new_call(self, phone: Phone) -> BaseCall:
        """A call on the configured voice provider (the plugin executes its call-control language)."""
        return self.voice.new_call(phone)

    def transcribe(self, text: str) -> str:
        """What the voice provider's speech recognition posts for ``text``: a punctuated sentence by
        default (:func:`simulators.speech.twilio_style`); ``transcriber`` models other engines."""
        from .speech import DEFAULT_STT_STYLE

        return (self.transcriber or DEFAULT_STT_STYLE)(text)

    # ----- webhooks (simulator -> app) -----

    def public_url(self, path: str) -> str:
        return f"{self._public}{path}"

    def _transport_url(self, url: str) -> str:
        if url.startswith(self._public):
            return self.app_url + url[len(self._public):]
        return url

    def _webhook(self, url: str, params: Optional[dict[str, str]], phone: Optional[str] = None, method: str = "POST",
                 kind: str = "webhook", expect_twiml: bool = True, raw: Optional[bytes] = None,
                 headers: Optional[dict[str, str]] = None, log_params: Optional[dict[str, Any]] = None) -> Optional[str]:
        """Send a webhook to the app: a form (``params``) or a raw body, with the plugin's ``headers``
        (signatures). Returns the body, or None if the provider would give up."""
        entry = LogEntry(kind=kind, method=method, url=url, params=dict(log_params or params or {}), phone=phone)
        started = time.monotonic()
        try:
            if raw is not None:
                resp = self.transport.request("POST", self._transport_url(url), content=raw, headers=headers or {},
                                              timeout=self.faults.webhook_timeout)
            elif method == "POST":
                resp = self.transport.request("POST", self._transport_url(url), data=params, headers=headers or {},
                                              timeout=self.faults.webhook_timeout)
            else:
                resp = self.transport.request("GET", self._transport_url(url), params=params, headers=headers or {},
                                              timeout=self.faults.webhook_timeout)
        except httpx.TimeoutException:
            entry.error = f"11205: webhook timed out after {self.faults.webhook_timeout:.0f}s"
            return self._log(entry, started, None)
        except httpx.HTTPError as exc:
            entry.error = f"11200: HTTP retrieval failure ({exc.__class__.__name__}: {exc})"
            return self._log(entry, started, None)
        entry.status, entry.response = resp.status_code, resp.text
        if resp.status_code >= 400:
            entry.error = f"11200: HTTP retrieval failure (status {resp.status_code})"
            return self._log(entry, started, None)
        if expect_twiml and "xml" not in resp.headers.get("content-type", ""):
            entry.error = f"12300: invalid content-type {resp.headers.get('content-type')!r}"
        return self._log(entry, started, resp.text)

    def _log(self, entry: LogEntry, started: float, result: Optional[str]) -> Optional[str]:
        entry.elapsed_ms = int((time.monotonic() - started) * 1000)
        if entry.error:
            log.warning("%s %s -> %s", entry.method, entry.url, entry.error)
        with self._lock:
            self.log.append(entry)
            self._changed()
        return result

    def note(self, text: str, phone: Optional[str] = None) -> None:
        log.info(text)
        with self._lock:
            self.log.append(LogEntry(kind="note", method="", url="", params={}, response=text, phone=phone))
            self._changed()

    # ----- WhatsApp delivery (every messaging plugin) -----

    def deliver_whatsapp(self, phone: Phone, body: str, *, via: str, provider: str, sid: str,
                         failed_code: Optional[int] = None, content_sid: Optional[str] = None,
                         status_callback: Optional[str] = None) -> WhatsAppMessage:
        """A business message accepted by the provider API: lands on the phone (or fails there),
        then delivery receipts go back to the app asynchronously (in the provider's format)."""
        message = WhatsAppMessage(sender="business", body=body, via=via, provider=provider, sid=sid,
                                  status="failed" if failed_code else "delivered", error_code=failed_code,
                                  content_sid=content_sid, in_reply=phone._exchanges > 0)
        message._status_callback = status_callback  # type: ignore[attr-defined]
        with self._lock:
            phone.thread.append(message)
            self._changed()
        statuses = [("sent", None), ("failed", failed_code)] if failed_code else [("sent", None), ("delivered", None)]
        for status, code in statuses:
            if self.faults.receipts_before_response:
                self._receipt(phone, message, status, code)
            else:
                self.dispatch(lambda status=status, code=code: self._receipt(phone, message, status, code))
        return message

    def _receipt(self, phone: Phone, message: WhatsAppMessage, status: str, code: Optional[int]) -> None:
        self.plugin(message.provider).receipt(phone, message, status, code)

    def candidate_read_everything(self, phone: Phone) -> None:
        """Writing back means the candidate read what we sent: read receipts for delivered messages."""
        with self._lock:
            unread = [m for m in phone.thread if m.sender == "business" and m.delivered and not m.read]
            for message in unread:
                message.read = True
            if unread:
                self._changed()
        for message in unread:
            self.dispatch(lambda message=message: self._receipt(phone, message, "read", None))

    def mark_read_by_business(self, message_id: str) -> None:
        with self._lock:
            for phone in self.phones.values():
                for message in phone.thread:
                    if message.sid == message_id and message.sender == "candidate":
                        message.read = True
                        self._changed()
                        return

    def store_media(self, data: bytes, mime: str, transcript: Optional[str] = None, voice: bool = False) -> Media:
        media = Media(data=data, mime=mime, transcript=transcript, voice=voice)
        with self._lock:
            self.media[media.id] = media
        return media

    # ----- UI -----

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "config": {"account_sid": self.twilio.account_sid, "whatsapp_number": self.whatsapp_number,
                           "voice_number": self.voice_number, "app_url": self.app_url,
                           "public_url": self._public, "signing": self.sign,
                           "whatsapp_provider": self.whatsapp_provider, "voice_provider": self.voice_provider,
                           "meta_phone_number_id": self.meta.phone_number_id},
                "faults": asdict(self.faults),
                "templates": {**self.twilio.templates, **self.meta.templates},
                "phones": [p.to_json() for p in self.phones.values()],
                "log": [e.to_json() for e in self.log],
            }
