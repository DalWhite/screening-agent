"""Vonage Voice API, simulated (``voice_provider="vonage"``).

What it reproduces:

* Answer webhook: ``GET {answer_url}?from=&to=&uuid=&conversation_uuid=``
  (numbers without "+"), answered with an NCCO (a JSON list of actions).
* NCCO execution: ``talk`` is read out; ``input`` with ``type: ["speech"]``
  listens; the result goes as JSON to its ``eventUrl``: ``speech.results``
  (text + confidence) or ``speech.timeout_reason = "start_timeout"`` with no
  results when the caller stays silent; the answer is the next NCCO. When an
  NCCO runs out of actions without an ``input`` the call ends.
* Event webhook: JSON ``status`` events (``answered``, ``completed``).
* Signed webhooks: ``Authorization: Bearer <JWT>`` signed HS256 with the
  application's signature secret, with ``payload_hash`` (SHA-256 of the body).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin

from .core import BaseCall

if TYPE_CHECKING:
    from .core import ChannelSimulator, Phone

DEFAULT_SIGNATURE_SECRET = "simulated-vonage-signature-secret"
DEFAULT_APPLICATION_ID = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab"
ANSWER_PATH = "/api/channels/voice/vonage/answer"
EVENT_PATH = "/api/channels/voice/vonage/event"
MAX_ACTIONS_WITHOUT_INPUT = 50


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_jwt(secret: str, body: bytes = b"", application_id: str = DEFAULT_APPLICATION_ID,
               issued_at: Optional[int] = None) -> str:
    """The JWT Vonage puts in ``Authorization: Bearer`` on signed webhooks."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload: dict[str, Any] = {"iat": int(issued_at if issued_at is not None else time.time()),
                               "jti": str(uuid.uuid4()), "iss": "Vonage", "application_id": application_id}
    if body:
        payload["payload_hash"] = hashlib.sha256(body).hexdigest()
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


class VonageSim:
    """The Vonage plugin (voice): configuration + call factory."""

    def __init__(self, sim: "ChannelSimulator", signature_secret: str = DEFAULT_SIGNATURE_SECRET,
                 sign: bool = True, answer_path: str = ANSWER_PATH, event_path: str = EVENT_PATH):
        self.sim = sim
        self.signature_secret = signature_secret
        self.sign = sign
        self.answer_path = answer_path
        self.event_path = event_path

    def headers(self, body: bytes = b"") -> dict[str, str]:
        headers = {"User-Agent": "Vonage-Voice/1.0"}
        if body:
            headers["Content-Type"] = "application/json"
        if self.sign:
            headers["Authorization"] = f"Bearer {signed_jwt(self.signature_secret, body)}"
        return headers

    def new_call(self, phone: "Phone") -> "VonageCall":
        return VonageCall(self.sim, phone, self)


class VonageCall(BaseCall):
    provider = "vonage"
    id_prefix = ""

    def __init__(self, sim: "ChannelSimulator", phone: "Phone", vonage: VonageSim):
        super().__init__(sim, phone)
        self.vonage = vonage
        self.sid = str(uuid.uuid4())  # Vonage call uuid
        self.conversation_uuid = "CON-" + str(uuid.uuid4())
        self._input: Optional[dict[str, Any]] = None  # the open "input" action
        self._input_url = ""

    # ----- provider hooks -----

    def _listening_language(self) -> Optional[str]:
        if self._input is None:
            return None
        return (self._input.get("speech") or {}).get("language") or "en-US"

    def _stop_listening(self) -> None:
        self._input = None

    def _common(self) -> dict[str, Any]:
        return {"uuid": self.sid, "conversation_uuid": self.conversation_uuid,
                "from": digits(self.phone.number), "to": digits(self.sim.voice_number),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}

    def _start(self) -> None:
        with self._lock:
            self.state = "in-progress"
            params = {k: v for k, v in self._common().items() if k != "timestamp"}
            url = self.sim.public_url(self.vonage.answer_path)
            body = self.sim._webhook(url, params, phone=self.phone.number, method="GET",
                                     headers=self.vonage.headers(), expect_twiml=False)
            self._event("answered")
            self._run_response(body, url)

    def _send_speech(self, speech: str, confidence: float) -> None:
        self._post_input({"results": [{"text": speech, "confidence": f"{confidence:.2f}"}],
                          "timeout_reason": "end_on_silence_timeout"})

    def _send_silence(self) -> None:
        self._post_input({"timeout_reason": "start_timeout", "results": []})

    def _post_input(self, speech: dict[str, Any]) -> None:
        url = self._input_url
        self._stop_listening()
        payload = {**self._common(), "speech": speech, "dtmf": {"digits": None, "timed_out": False}}
        raw = json.dumps(payload).encode()
        body = self.sim._webhook(url, None, phone=self.phone.number, raw=raw, headers=self.vonage.headers(raw),
                                 expect_twiml=False, log_params=payload)
        self._run_response(body, url)

    def _status_callback(self) -> None:
        if self.sim.send_status_callbacks:
            duration = int((self.ended_at - self.started_at).total_seconds()) if self.ended_at else 0
            self._event("completed", duration=str(duration))

    def _event(self, status: str, **extra: Any) -> None:
        payload = {**self._common(), "status": status, "direction": "inbound", **extra}
        raw = json.dumps(payload).encode()
        self.sim._webhook(self.sim.public_url(self.vonage.event_path), None, phone=self.phone.number, raw=raw,
                          headers=self.vonage.headers(raw), kind="status_callback", expect_twiml=False,
                          log_params=payload)

    # ----- NCCO execution -----

    def _run_response(self, body: Optional[str], url: str) -> None:
        if body is None:
            return self._application_error("webhook failed")
        try:
            ncco = json.loads(body)
        except ValueError:
            return self._application_error("the answer is not an NCCO (JSON list)")
        if not isinstance(ncco, list):
            return self._application_error("the answer is not an NCCO (JSON list)")
        self._run(ncco, url)

    def _run(self, ncco: list[dict[str, Any]], url: str) -> None:
        for index, action in enumerate(ncco[:MAX_ACTIONS_WITHOUT_INPUT]):
            kind = action.get("action")
            if kind == "talk":
                self._agent_says(str(action.get("text") or ""), action.get("language"))
            elif kind == "input":
                if "speech" not in (action.get("type") or []):
                    self.sim.note(f"call {self.sid}: input without speech (type={action.get('type')})",
                                  phone=self.phone.number)
                event_url = (action.get("eventUrl") or [""])[0]
                if not event_url:
                    return self._application_error("input action without eventUrl")
                self._input, self._input_url = action, urljoin(url, event_url)
                if ncco[index + 1:]:
                    self.sim.note(f"call {self.sid}: actions after input are ignored", phone=self.phone.number)
                return
            else:
                self.sim.note(f"call {self.sid}: unsupported NCCO action {kind!r} skipped", phone=self.phone.number)
        self._end("hangup-by-app")  # the NCCO ran out of actions: Vonage ends the call
