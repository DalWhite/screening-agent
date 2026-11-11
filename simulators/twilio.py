"""Twilio, simulated: WhatsApp (form webhooks + TwiML + REST API) and Programmable Voice (TwiML).

* **Towards the app** it behaves like Twilio's webhooks: form-encoded POSTs
  signed with ``X-Twilio-Signature``, and it *executes* the TwiML the app
  answers: ``<Message>`` bodies land in the candidate's WhatsApp thread;
  ``<Say>``, ``<Gather>``, ``<Redirect>``, ``<Hangup>`` drive a call the way
  Twilio would (silence falls through to ``<Redirect>``, the end of the call
  sends the status callback).
* **Towards the app's outbound code** it is the Twilio REST API
  (``Messages.create``, media, account): authentication, sender checks, the
  24h window, template-only senders and outages with Twilio's error codes,
  status callbacks (``sent`` / ``delivered`` / ``read`` / ``undelivered``).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlsplit

from twilio.http import HttpClient
from twilio.http.response import Response as TwilioResponse
from twilio.request_validator import RequestValidator

from . import twiml as T
from .core import BaseCall, LogEntry, Media, Phone, WhatsAppMessage, _now, _sid, e164

if TYPE_CHECKING:
    from .core import ChannelSimulator

log = logging.getLogger("simulators.twilio")

DEFAULT_ACCOUNT_SID = "AC" + "5" * 32  # AC + 32 hex chars, like a real account SID
DEFAULT_AUTH_TOKEN = "simulated-auth-token"
API_VERSION = "2010-04-01"
MAX_REDIRECTS_WITHOUT_INPUT = 10  # a <Redirect> loop the caller can never leave is an app bug


def twilio_error(status: int, code: int, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"code": code, "message": message, "more_info": f"https://www.twilio.com/docs/errors/{code}",
                    "status": status}


class TwilioSim:
    """The Twilio plugin: one account, the simulator's WhatsApp sender and voice number."""

    def __init__(self, sim: "ChannelSimulator", account_sid: str = DEFAULT_ACCOUNT_SID,
                 auth_token: str = DEFAULT_AUTH_TOKEN):
        self.sim = sim
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.api_keys: dict[str, str] = {}  # SK... -> secret accepted for REST auth
        self.templates: dict[str, str] = {}  # Content SID -> body with {{1}}, {{2}} placeholders
        self.rest_messages: list[dict[str, Any]] = []  # every Messages.create the app made

    # ----- webhooks (simulator -> app) -----

    def signature(self, url: str, params: dict[str, str]) -> str:
        return RequestValidator(self.auth_token).compute_signature(url, params)

    def post_form(self, url: str, params: dict[str, str], phone: Optional[str] = None, method: str = "POST",
                  kind: str = "webhook", expect_twiml: bool = True) -> Optional[str]:
        """A webhook the way Twilio sends it: a form, signed with X-Twilio-Signature."""
        headers = {"User-Agent": "TwilioProxy/1.1", "I-Twilio-Idempotency-Token": secrets.token_hex(8)}
        if self.sim.sign:
            headers["X-Twilio-Signature"] = self.signature(url, params if method == "POST" else {})
        return self.sim._webhook(url, params, phone=phone, method=method, kind=kind, expect_twiml=expect_twiml,
                                 headers=headers)

    # ----- WhatsApp (messaging plugin interface) -----

    def new_message_id(self) -> str:
        return _sid("SM")

    def inbound(self, phone: Phone, message: WhatsAppMessage, attachment: Optional[Media]) -> list[str]:
        """The candidate's message as Twilio's form webhook; the replies are the TwiML <Message>s."""
        params = {
            "MessageSid": message.sid, "SmsMessageSid": message.sid, "SmsSid": message.sid,
            "AccountSid": self.account_sid, "ApiVersion": API_VERSION,
            "From": f"whatsapp:{phone.number}", "To": f"whatsapp:{self.sim.whatsapp_number}",
            "Body": message.body, "NumSegments": "1", "SmsStatus": "received",
            "ProfileName": phone.name or "Candidato", "WaId": phone.number.lstrip("+"),
        }
        urls = [(url, "image/jpeg") for url in message.media]
        if attachment is not None:
            urls.append((f"https://api.twilio.com/{API_VERSION}/Accounts/{self.account_sid}/Messages/"
                         f"{message.sid}/Media/ME{attachment.id}", attachment.mime))
        params["NumMedia"] = str(len(urls))
        for i, (url, mime) in enumerate(urls):
            params[f"MediaUrl{i}"] = url
            params[f"MediaContentType{i}"] = mime
        phone._last_inbound = params
        return self._post(phone, params)

    def resend(self, phone: Phone, params: dict[str, str]) -> list[str]:
        return self._post(phone, params)

    def _post(self, phone: Phone, params: dict[str, str]) -> list[str]:
        xml = self.post_form(self.sim.public_url(self.sim.paths.whatsapp_inbound), params, phone=phone.number)
        if xml is None:
            return []
        try:
            bodies = T.parse_messaging(xml)
        except T.TwiMLError as exc:
            self.sim.note(f"whatsapp reply to {phone.number} discarded: {exc}", phone=phone.number)
            return []
        with self.sim._lock:
            for text in bodies:
                phone.thread.append(WhatsAppMessage(sender="business", body=text, via="twiml", in_reply=True))
            self.sim._changed()
        return bodies

    def receipt(self, phone: Phone, message: WhatsAppMessage, status: str, code: Optional[int]) -> None:
        """Status callback to the URL the app gave in Messages.create (if it gave one)."""
        url = getattr(message, "_status_callback", None)
        if not url or not self.sim.send_status_callbacks:
            return
        twilio_status = "undelivered" if status == "failed" else status
        params = {"MessageSid": message.sid, "SmsSid": message.sid, "MessageStatus": twilio_status,
                  "SmsStatus": twilio_status, "AccountSid": self.account_sid, "ApiVersion": API_VERSION,
                  "From": f"whatsapp:{self.sim.whatsapp_number}", "To": f"whatsapp:{phone.number}",
                  "ChannelPrefix": "whatsapp"}
        if code:
            params["ErrorCode"] = str(code)
        self.post_form(url, params, phone=phone.number, kind="status_callback", expect_twiml=False)

    # ----- voice (voice plugin interface) -----

    def new_call(self, phone: Phone) -> "TwilioCall":
        return TwilioCall(self.sim, phone)

    # ----- the REST API (app -> simulator) -----

    def handle_rest(self, method: str, path: str, params: dict[str, Any],
                    auth: Optional[tuple[str, str]]) -> tuple[int, dict[str, Any]]:
        """Serve one Twilio REST request. Returns ``(status, json_body)``."""
        entry = LogEntry(kind="rest", method=method.upper(), url=path, params=dict(params))
        started = time.monotonic()
        status, body = self._route_rest(method.upper(), path, params, auth)
        entry.status, entry.response = status, json.dumps(body, ensure_ascii=False)[:2000]
        entry.phone = body.get("_phone") if isinstance(body, dict) else None
        body.pop("_phone", None)
        if status >= 400:
            entry.error = f"{body.get('code')}: {body.get('message')}"
        self.sim._log(entry, started, None)
        return status, body

    def _authorized(self, auth: Optional[tuple[str, str]]) -> bool:
        if not auth:
            return False
        user, password = auth
        if user == self.account_sid:
            return secrets.compare_digest(password, self.auth_token)
        return user in self.api_keys and secrets.compare_digest(password, self.api_keys[user])

    def _route_rest(self, method: str, path: str, params: dict[str, Any],
                    auth: Optional[tuple[str, str]]) -> tuple[int, dict[str, Any]]:
        if self.sim.faults.rest_down:
            return twilio_error(503, 20503, "Service Unavailable")
        match = re.fullmatch(rf"/{API_VERSION}/Accounts/(AC\w+)(/Messages)?(?:/(SM\w+))?\.json", path)
        if match is None:
            return twilio_error(404, 20404, f"The requested resource {path} was not found")
        account, messages, message_sid = match.groups()
        if not self._authorized(auth):
            return twilio_error(401, 20003, "Authenticate")
        if account != self.account_sid:
            return twilio_error(404, 20404, f"The requested resource {path} was not found")
        if not messages:
            return 200, self._account_resource()
        if method == "POST" and not message_sid:
            return self._create_message(params)
        if method == "GET" and message_sid:
            found = next((m for m in self.rest_messages if m["sid"] == message_sid), None)
            return (200, found) if found else twilio_error(404, 20404, f"The requested resource {path} was not found")
        if method == "GET":
            return 200, {"messages": list(reversed(self.rest_messages))[:50]}
        return twilio_error(405, 20004, "Method not allowed")

    def _account_resource(self) -> dict[str, Any]:
        return {"sid": self.account_sid, "friendly_name": "Screening agent (simulated)", "status": "active",
                "type": "Full", "date_created": _now().isoformat()}

    def _create_message(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        to, from_ = str(params.get("To") or ""), str(params.get("From") or "")
        body, content_sid = str(params.get("Body") or ""), params.get("ContentSid")
        if not to:
            return twilio_error(400, 21604, "A 'To' phone number is required.")
        if not from_.startswith("whatsapp:"):
            return twilio_error(400, 21910, "Invalid From and To pair. From and To should be of the same channel")
        if not to.startswith("whatsapp:"):
            return twilio_error(400, 21910, "Invalid From and To pair. From and To should be of the same channel")
        try:
            sender, number = e164(from_), e164(to)
        except ValueError:
            return twilio_error(400, 21211, f"Invalid 'To' Phone Number: {to}")
        if sender != self.sim.whatsapp_number:
            return twilio_error(400, 63007, f"Twilio could not find a Channel with the specified From address: {from_}")
        if not body and not content_sid:
            return twilio_error(400, 21619, "A text message Body, Media Url or Content Sid is required")

        phone = self.sim.phone(number)
        error_code, via = None, "rest"
        if content_sid:
            try:
                variables = json.loads(params.get("ContentVariables") or "{}")
            except ValueError:
                return twilio_error(400, 21656, "The Content Variables parameter is invalid")
            body, via = self.render_template(str(content_sid), variables), "template"
        else:
            if self.sim.faults.template_only:
                return twilio_error(400, 21654, "ContentSid Required")
            if self.sim.faults.enforce_window and not phone.window_open():
                if self.sim.faults.window_errors == "sync":
                    return twilio_error(400, 63016, "Failed to send freeform message because you are outside "
                                                    "the allowed window. Please use a Template.")
                error_code = 63016  # accepted, then fails without reaching the phone

        message = self.sim.deliver_whatsapp(phone, body, via=via, provider="twilio", sid=_sid("SM"),
                                        failed_code=error_code, content_sid=str(content_sid) if content_sid else None,
                                        status_callback=str(params.get("StatusCallback") or "") or None)
        resource = {
            "sid": message.sid, "account_sid": self.account_sid, "to": to, "from": from_, "body": body,
            "status": "queued", "direction": "outbound-api", "num_segments": "1", "num_media": "0",
            "error_code": None, "error_message": None, "content_sid": message.content_sid,
            "api_version": API_VERSION, "date_created": message.at.isoformat(),
            "uri": f"/{API_VERSION}/Accounts/{self.account_sid}/Messages/{message.sid}.json",
        }
        with self.sim._lock:
            self.rest_messages.append({**resource, "status": message.status, "error_code": error_code})
        return 201, {**resource, "_phone": number}

    def media(self, path: str, auth: Optional[tuple[str, str]]) -> tuple[int, Any]:
        """``GET /2010-04-01/Accounts/{AC}/Messages/{MM}/Media/{ME}`` (Basic auth, like Twilio)."""
        entry = LogEntry(kind="rest", method="GET", url=path, params={})
        started = time.monotonic()
        match = re.fullmatch(rf"/{API_VERSION}/Accounts/(AC\w+)/Messages/(\w+)/Media/ME(\w+)", path)
        media = self.sim.media.get(match.group(3)) if match else None
        if self.sim.faults.rest_down:
            status, result = twilio_error(503, 20503, "Service Unavailable")
        elif not self._authorized(auth):
            status, result = twilio_error(401, 20003, "Authenticate")
        elif media is None:
            status, result = twilio_error(404, 20404, f"The requested resource {path} was not found")
        else:
            status, result = 200, media
        entry.status = status
        entry.response = f"{len(media.data)} bytes {media.mime}" if status == 200 else json.dumps(result)
        self.sim._log(entry, started, None)
        return status, result

    def render_template(self, content_sid: str, variables: dict[str, Any]) -> str:
        template = self.templates.get(content_sid)
        if template is None:
            values = ", ".join(f"{{{{{k}}}}}={v}" for k, v in sorted(variables.items()))
            return f"[plantilla {content_sid}] {values}".strip()
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(variables.get(m.group(1), m.group(0))), template)

    def rest_client(self):
        """A real ``twilio.rest.Client`` whose HTTP layer is this simulator (in-process, no sockets)."""
        from twilio.rest import Client

        return Client(self.account_sid, self.auth_token, http_client=SimulatorHttpClient(self))



class TwilioCall(BaseCall):
    """A call executed the way Twilio executes TwiML.

    After every webhook answer the call runs verbs until it needs the caller
    (a ``<Gather>``) or ends (``<Hangup>``, end of document, application error).
    """

    provider = "twilio"

    def __init__(self, sim: "ChannelSimulator", phone: "Phone"):
        super().__init__(sim, phone)
        self._gather: Optional[T.Gather] = None
        self._gather_doc_url: str = ""
        self._remaining: list[T.Verb] = []  # verbs after the pending <Gather> (run on silence)

    def _listening_language(self) -> Optional[str]:
        if self._gather is None:
            return None
        return self._gather.language or "en-US"

    def _stop_listening(self) -> None:
        self._gather, self._remaining = None, []

    def _start(self) -> None:
        with self._lock:
            url = self.sim.public_url(self.sim.paths.voice_inbound)
            self._fetch_and_run(url, self._params("ringing"), "POST")

    def _send_speech(self, speech: str, confidence: float) -> None:
        gather, doc_url = self._gather, self._gather_doc_url
        self._stop_listening()
        url = urljoin(doc_url, gather.action) if gather.action else doc_url
        params = {**self._params("in-progress"), "SpeechResult": speech, "Confidence": f"{confidence:.2f}"}
        self._fetch_and_run(url, params, gather.method)

    def _send_silence(self) -> None:
        remaining, doc_url = self._remaining, self._gather_doc_url
        self._stop_listening()
        self._run(remaining, doc_url, redirects=0)  # Twilio falls through to the next verb

    def _params(self, status: str) -> dict[str, str]:
        return {
            "AccountSid": self.sim.twilio.account_sid, "ApiVersion": API_VERSION, "CallSid": self.sid,
            "CallStatus": status, "Direction": "inbound", "From": self.phone.number, "To": self.sim.voice_number,
            "Caller": self.phone.number, "Called": self.sim.voice_number,
        }

    def _fetch_and_run(self, url: str, params: dict[str, str], method: str) -> None:
        self.state = "in-progress"
        response = self.sim.twilio.post_form(url, params, phone=self.phone.number, method=method)
        if response is None:
            return self._application_error("webhook failed")
        try:
            verbs = T.parse_voice(response)
        except T.TwiMLError as exc:
            return self._application_error(str(exc))
        self._run(verbs, url, redirects=0)

    def _run(self, verbs: list[T.Verb], doc_url: str, redirects: int) -> None:
        for index, verb in enumerate(verbs):
            if isinstance(verb, T.Say):
                self._agent_says(verb.text, verb.language)
            elif isinstance(verb, T.Pause):
                continue
            elif isinstance(verb, T.Gather):
                for say in verb.says:
                    self._agent_says(say.text, say.language)
                if "speech" not in verb.input:
                    self.sim.note(f"call {self.sid}: <Gather input={verb.input!r}> (no speech): treated as speech",
                                  phone=self.phone.number)
                self._gather, self._gather_doc_url, self._remaining = verb, doc_url, list(verbs[index + 1:])
                return
            elif isinstance(verb, T.Redirect):
                if redirects >= MAX_REDIRECTS_WITHOUT_INPUT:
                    return self._application_error("redirect loop without caller input")
                url = urljoin(doc_url, verb.url)
                response = self.sim.twilio.post_form(url, self._params("in-progress"), phone=self.phone.number,
                                             method=verb.method)
                if response is None:
                    return self._application_error("webhook failed")
                try:
                    next_verbs = T.parse_voice(response)
                except T.TwiMLError as exc:
                    return self._application_error(str(exc))
                return self._run(next_verbs, url, redirects + 1)  # verbs after <Redirect> never run
            elif isinstance(verb, T.Hangup):
                return self._end("hangup-by-app")
            else:
                self.sim.note(f"call {self.sid}: unsupported TwiML verb <{verb.tag}> skipped",
                              phone=self.phone.number)
        self._end("end-of-twiml")  # Twilio hangs up when the document runs out of verbs

    def _status_callback(self) -> None:
        if self.sim.send_status_callbacks:
            duration = int((self.ended_at - self.started_at).total_seconds())
            params = {**self._params("completed"), "CallDuration": str(duration)}
            self.sim.twilio.post_form(self.sim.public_url(self.sim.paths.voice_status), params, phone=self.phone.number,
                              kind="status_callback", expect_twiml=False)


class SimulatorHttpClient(HttpClient):
    """Twilio SDK HTTP layer that hands every request to the simulated REST API (in-process, no sockets)."""

    def __init__(self, twilio: "TwilioSim"):
        super().__init__(logging.getLogger("simulators.twilio.http"), False)
        self.twilio = twilio

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None,
                allow_redirects=False) -> TwilioResponse:
        status, body = self.twilio.handle_rest(method, urlsplit(url).path, {**(params or {}), **(data or {})}, auth)
        return TwilioResponse(status, json.dumps(body))


def basic_auth(header: str) -> Optional[tuple[str, str]]:
    """Decode an ``Authorization: Basic ...`` header."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return None
    return user, password
