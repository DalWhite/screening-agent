"""The Twilio simulator itself: TwiML execution, REST API fidelity, signing.

These tests run the simulator against a tiny fake app (not the screening
app), so a failure here means the simulator, not the product, is wrong.
"""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from simulators import twiml
from simulators.server import create_app
from simulators.core import ChannelSimulator, SimulationError
from simulators.twilio import DEFAULT_ACCOUNT_SID, DEFAULT_AUTH_TOKEN

NUMBER = "+34600111222"


# ---------- TwiML parsing ----------

def test_parse_messaging_reads_every_message_body():
    xml = '<?xml version="1.0"?><Response><Message>uno</Message><Message><Body>dos</Body></Message></Response>'
    assert twiml.parse_messaging(xml) == ["uno", "dos"]
    assert twiml.parse_messaging("<Response/>") == []


@pytest.mark.parametrize("bad", ["", "not xml", "<Foo/>"])
def test_invalid_twiml_is_rejected_like_twilio(bad):
    with pytest.raises(twiml.TwiMLError):
        twiml.parse_messaging(bad)


def test_parse_voice_verbs():
    xml = ('<Response><Gather input="speech" language="en-US" action="/collect?x=1" method="POST" '
           'speechTimeout="auto"><Say language="en-US">Hi</Say></Gather>'
           '<Redirect method="POST">/collect?x=1</Redirect><Hangup/><Play>x.mp3</Play></Response>')
    gather, redirect, hangup, play = twiml.parse_voice(xml)
    assert gather.action == "/collect?x=1" and gather.language == "en-US" and gather.input == "speech"
    assert gather.says[0].text == "Hi"
    assert redirect.url == "/collect?x=1" and isinstance(hangup, twiml.Hangup)
    assert isinstance(play, twiml.Unsupported) and play.tag == "Play"


# ---------- a fake app to drive the simulator ----------

def fake_app(calls: list[dict]) -> FastAPI:
    """Two-question IVR + a WhatsApp echo bot, written directly in TwiML."""
    app = FastAPI()
    validator = RequestValidator(DEFAULT_AUTH_TOKEN)

    async def record(request: Request) -> dict:
        form = {k: str(v) for k, v in (await request.form()).items()}
        ok = validator.validate(str(request.url), form, request.headers.get("X-Twilio-Signature", ""))
        calls.append({"path": request.url.path, "query": request.url.query, "form": form, "signed": ok})
        return form

    def xml(body: str) -> Response:
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                        media_type="application/xml")

    @app.post("/wa")
    async def wa(request: Request):
        form = await record(request)
        if form["Body"] == "boom":
            return Response("oops", status_code=500)
        return xml(f"<Message>eco: {form['Body']}</Message><Message>otra</Message>")

    @app.post("/call")
    async def call(request: Request):
        await record(request)
        return xml('<Say language="es-ES">Bienvenido</Say><Gather input="speech" language="es-ES" '
                   'action="/answer?step=1"><Say language="es-ES">¿Nombre?</Say></Gather>'
                   '<Redirect method="POST">/answer?step=1</Redirect>')

    @app.post("/answer")
    async def answer(request: Request, step: int):
        form = await record(request)
        if not form.get("SpeechResult"):
            return xml('<Say>Adiós</Say><Hangup/>')
        if step == 1:
            return xml('<Gather input="speech" language="en-US" action="/answer?step=2"><Say>Age?</Say></Gather>')
        if form["SpeechResult"] == "loop":
            return xml('<Redirect>/loop</Redirect>')
        return xml('<Say>Gracias</Say>')  # no <Hangup/>: Twilio ends the call at the end of the document

    @app.post("/loop")
    async def loop(request: Request):
        await record(request)
        return xml('<Pause length="1"/><Redirect>/loop</Redirect>')  # never listens: a stuck call

    @app.post("/status")
    async def status(request: Request):
        await record(request)
        return Response(status_code=204)

    return app


@pytest.fixture
def calls():
    return []


@pytest.fixture
def sim(calls):
    from simulators.core import ChannelPaths

    client = TestClient(fake_app(calls))
    simulator = ChannelSimulator("http://testserver", transport=client, transcriber=lambda text: text,  # exact text
                                paths=ChannelPaths(whatsapp_inbound="/wa", voice_inbound="/call", voice_status="/status"))
    yield simulator
    client.close()


# ---------- webhooks ----------

def test_whatsapp_webhook_is_signed_and_shaped_like_twilio(sim, calls):
    phone = sim.phone(NUMBER, "Laura")
    assert phone.whatsapp("hola") == ["eco: hola", "otra"]
    form = calls[0]["form"]
    assert calls[0]["signed"] is True  # the real RequestValidator accepts our signature
    assert form["From"] == f"whatsapp:{NUMBER}" and form["To"] == "whatsapp:+14155238886"
    assert form["AccountSid"] == DEFAULT_ACCOUNT_SID and form["MessageSid"].startswith("SM")
    assert form["WaId"] == NUMBER[1:] and form["ProfileName"] == "Laura" and form["NumMedia"] == "0"
    assert [m.sender for m in phone.thread] == ["candidate", "business", "business"]
    assert phone.window_open()


def test_media_message_carries_media_params(sim, calls):
    sim.phone(NUMBER).whatsapp("", media=["https://example.test/dni.jpg"])
    form = calls[0]["form"]
    assert form["NumMedia"] == "1" and form["MediaUrl0"] == "https://example.test/dni.jpg"


def test_failing_webhook_is_logged_as_11200_and_delivers_nothing(sim):
    phone = sim.phone(NUMBER)
    assert phone.whatsapp("boom") == []
    assert phone.received() == []
    assert sim.log[-1].error.startswith("11200")


def test_wrong_auth_token_produces_signatures_the_app_rejects(calls):
    from simulators.core import ChannelPaths

    client = TestClient(fake_app(calls))
    sim = ChannelSimulator("http://testserver", transport=client, twilio={"auth_token": "another-token"},
                          paths=ChannelPaths(whatsapp_inbound="/wa"))
    sim.phone(NUMBER).whatsapp("hola")
    assert calls[0]["signed"] is False


# ---------- voice: TwiML execution ----------

def test_call_runs_twiml_until_the_gather(sim, calls):
    call = sim.phone(NUMBER).call()
    assert call.waiting and call.gather_language == "es-ES"
    assert call.prompt == "Bienvenido ¿Nombre?"
    assert calls[0]["form"]["CallStatus"] == "ringing" and calls[0]["form"]["CallSid"] == call.sid

    assert call.say("Laura") == "Age?"
    assert call.gather_language == "en-US"
    posted = calls[-1]
    assert posted["path"] == "/answer" and posted["query"] == "step=1"  # relative action resolved like Twilio
    assert posted["form"]["SpeechResult"] == "Laura" and posted["signed"]


def test_end_of_document_ends_the_call_and_sends_the_status_callback(sim, calls):
    call = sim.phone(NUMBER).call()
    call.say("Laura")
    call.say("30")
    assert not call.active and call.end_reason == "end-of-twiml"
    assert call.agent_lines()[-1] == "Gracias"
    status = calls[-1]
    assert status["path"] == "/status" and status["form"]["CallStatus"] == "completed" and status["signed"]
    with pytest.raises(SimulationError):
        call.say("¿hola?")


def test_silence_falls_through_to_the_redirect(sim, calls):
    call = sim.phone(NUMBER).call()
    call.silence()
    assert "SpeechResult" not in calls[1]["form"] and calls[1]["path"] == "/answer"
    assert call.end_reason == "hangup-by-app" and call.agent_lines()[-1] == "Adiós"


def test_redirect_loop_is_reported_as_application_error(sim):
    call = sim.phone(NUMBER).call()
    call.say("Laura")
    call.say("loop")
    assert call.end_reason == "application-error"
    assert any("redirect loop" in (e.response or "") for e in sim.log if e.kind == "note")


def test_caller_hangup_sends_completed_status(sim, calls):
    call = sim.phone(NUMBER).call()
    call.hangup()
    assert call.end_reason == "caller-hangup" and calls[-1]["path"] == "/status"


def test_default_speech_results_look_like_twilios():
    from simulators import speech

    assert speech.twilio_style("sí, tengo 34 años") == "Sí, tengo 34 años."
    assert speech.twilio_style("¿hay alguien?") == "Hay alguien?"
    assert speech.chain(speech.digit_by_digit, speech.plain_lowercase)("Mi DNI es 12345678Z.") == "mi dni es 1 2 3 4 5 6 7 8 z"
    assert ChannelSimulator("http://x", transport=object()).transcribe("laura") == "Laura."


def test_transcriber_hook_models_speech_recognition(calls):
    from simulators.core import ChannelPaths

    client = TestClient(fake_app(calls))
    sim = ChannelSimulator("http://testserver", transport=client, transcriber=str.lower,
                          paths=ChannelPaths(voice_inbound="/call", voice_status="/status"))
    sim.phone(NUMBER).call().say("LAURA")
    assert calls[-1]["form"]["SpeechResult"] == "laura"


# ---------- REST API (what the app's SDK talks to) ----------

@pytest.fixture
def rest(sim):
    return sim.twilio.rest_client()


def send(rest, body="hola", to=NUMBER, from_="whatsapp:+14155238886", **kwargs):
    return rest.messages.create(to=f"whatsapp:{to}", from_=from_, body=body, **kwargs)


def test_rest_message_lands_in_the_phone_thread(sim, rest):
    sim.phone(NUMBER).whatsapp("hola")  # opens the 24h window
    message = send(rest, "¿Sigues ahí?")
    assert message.sid.startswith("SM") and message.status == "queued"
    assert sim.phone(NUMBER).texts()[-1] == "¿Sigues ahí?"
    assert sim.phone(NUMBER).thread[-1].via == "rest"


def test_rest_rejects_bad_credentials_with_20003(sim):
    from twilio.rest import Client

    from simulators.twilio import SimulatorHttpClient

    client = Client(DEFAULT_ACCOUNT_SID, "wrong", http_client=SimulatorHttpClient(sim.twilio))
    with pytest.raises(TwilioRestException) as err:
        send(client)
    assert err.value.code == 20003 and err.value.status == 401


def test_rest_rejects_unknown_sender_with_63007(sim, rest):
    with pytest.raises(TwilioRestException) as err:
        send(rest, from_="whatsapp:+10000000000")
    assert err.value.code == 63007


def test_freeform_outside_the_24h_window_fails_with_63016(sim, rest):
    with pytest.raises(TwilioRestException) as err:
        send(rest)  # the candidate never wrote: no window
    assert err.value.code == 63016
    phone = sim.phone(NUMBER)
    phone.whatsapp("hola")
    send(rest)
    phone.close_window()
    with pytest.raises(TwilioRestException):
        send(rest)


def test_async_window_errors_accept_then_fail_undelivered(sim, rest):
    sim.faults.window_errors = "async"
    message = send(rest)
    assert message.status == "queued"
    phone = sim.phone(NUMBER)
    assert phone.received() == [] and phone.received(delivered_only=False)[0].error_code == 63016
    assert rest.messages(message.sid).fetch().status == "failed"


def test_templates_are_rendered_with_their_variables(sim, rest):
    sim.twilio.templates["HX123"] = "Hola {{1}}, soy {{2}}. ¿Seguimos?"
    rest.messages.create(to=f"whatsapp:{NUMBER}", from_="whatsapp:+14155238886", content_sid="HX123",
                         content_variables=json.dumps({"1": "Laura", "2": "Marisol"}))
    msg = sim.phone(NUMBER).thread[-1]
    assert msg.body == "Hola Laura, soy Marisol. ¿Seguimos?" and msg.via == "template"


def test_template_only_sender_refuses_freeform_with_21654(sim, rest):
    sim.faults.template_only = True
    sim.phone(NUMBER).whatsapp("hola")
    with pytest.raises(TwilioRestException) as err:
        send(rest)
    assert err.value.code == 21654


def test_outage_answers_503(sim, rest):
    sim.faults.rest_down = True
    with pytest.raises(TwilioRestException) as err:
        send(rest)
    assert err.value.status == 503


def test_account_fetch_supports_the_credentials_check(sim, rest):
    account = rest.api.v2010.accounts(DEFAULT_ACCOUNT_SID).fetch()
    assert account.status == "active"


# ---------- HTTP server (control API + REST over the wire) ----------

def test_server_serves_rest_and_control_api(sim):
    import base64

    with TestClient(create_app(sim)) as http:
        assert http.post("/sim/api/phones", json={"number": NUMBER, "name": "Laura"}).status_code == 200
        assert http.post(f"/sim/api/phones/{NUMBER}/whatsapp", json={"body": "hola"}).json()["replies"][0] == "eco: hola"
        auth = "Basic " + base64.b64encode(f"{DEFAULT_ACCOUNT_SID}:{DEFAULT_AUTH_TOKEN}".encode()).decode()
        resp = http.post(f"/2010-04-01/Accounts/{DEFAULT_ACCOUNT_SID}/Messages.json",
                         data={"To": f"whatsapp:{NUMBER}", "From": "whatsapp:+14155238886", "Body": "push"},
                         headers={"Authorization": auth})
        assert resp.status_code == 201 and resp.json()["sid"].startswith("SM")
        assert http.post(f"/2010-04-01/Accounts/{DEFAULT_ACCOUNT_SID}/Messages.json", data={}).json()["code"] == 20003

        call = http.post(f"/sim/api/phones/{NUMBER}/calls").json()
        assert call["waiting"] and call["prompt"] == "Bienvenido ¿Nombre?"
        assert http.post(f"/sim/api/phones/{NUMBER}/calls").status_code == 409  # one call at a time
        after = http.post(f"/sim/api/calls/{call['sid']}/speech", json={"text": "Laura"}).json()
        assert after["prompt"] == "Age?"
        assert http.post(f"/sim/api/calls/{call['sid']}/hangup").json()["state"] == "completed"
        assert http.post(f"/sim/api/calls/{call['sid']}/silence").status_code == 409

        assert http.post("/sim/api/faults", json={"rest_down": True}).json()["rest_down"] is True
        assert http.post("/sim/api/faults", json={"nope": 1}).status_code == 422
        state = http.get("/sim/api/state").json()
        assert state["phones"][0]["thread"][-1]["body"] == "push"
        assert http.get("/").status_code == 200
