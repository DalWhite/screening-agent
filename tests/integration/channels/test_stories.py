"""End-to-end channel scenarios through the Twilio simulator.

The whole app runs in-process (TestClient, MockLLM, throwaway SQLite) and the
simulator plays Twilio on both sides: candidates write on WhatsApp and call
by phone through signed webhooks whose TwiML is executed like Twilio would,
and everything the app pushes goes through its real Twilio SDK client to the
simulator's REST API. Signature validation stays on.

Every WhatsApp story runs twice (the ``provider`` fixture): on Twilio and on
Meta's Cloud API, where replies are Graph API calls and delivery failures
arrive later in status webhooks.

Each test reads as a recruiting story: a candidate, one or more channels,
sometimes a recruiter, sometimes Twilio misbehaving.
"""

from __future__ import annotations

from backend.config import settings

LAURA = "+34600111222"
CARLOS = "+34600333444"

# Answers after the opening question (the WhatsApp trigger "hola" / the call greeting).
COURIER = ["sí, empecemos", "Laura Jiménez Ortiz", "sí, tengo carnet", "Madrid, Malasaña", "jornada completa",
           "mañana", "2 años en Glovo", "en 2 semanas"]
COURIER_EN = ["yes, let's start", "Laura Jimenez Ortiz", "yes, I have a driving licence", "Madrid, Malasaña",
              "full time", "mornings", "2 years with Glovo", "in 2 weeks"]


def construction(document: str = "12345678Z") -> list[str]:
    return ["sí", "Carlos Martín Ruiz", "sí, tengo 34 años", "sí, tengo NIE con permiso", f"mi DNI es {document}",
            "Getafe", "sí, sin problema", "no, no lo tengo", "3 años de peón", "tengo coche", "el lunes"]


# mock ID registry: 99… not in the registry, 97… valid but answers 6x slower
UNKNOWN_ID, SLOW_ID = "99000001C", "97000001P"


TEMPLATE_SETTINGS = {  # the re-engagement template, as each provider names it
    "twilio": {"twilio_whatsapp_content_sid": "HXreengage"},
    "meta": {"meta_template_name": "reengage_candidates"},
}


def install_template(stack, body="Hola {{1}}, soy {{2}}. ¿Seguimos con tu candidatura?"):
    stack.sim.twilio.templates["HXreengage"] = body
    stack.sim.meta.templates["reengage_candidates"] = body


def whatsapp_screening(phone, answers):
    phone.whatsapp("hola")
    return [phone.whatsapp(text) for text in answers]


def events(stack, session_id):
    return [e["type"] for e in stack.session(session_id)["events"]]


# ======================= WhatsApp =======================

def test_whatsapp_courier_screening_end_to_end(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA, "Laura")

    assert "Grupo Sazón" in laura.whatsapp("hola")[0]
    replies = [laura.whatsapp(text) for text in COURIER]

    assert "nombre" in replies[0][0].lower()
    assert all(len(r) == 1 for r in replies)  # one answer per message, all inside the webhook response
    assert all(m.in_reply for m in laura.received())  # happy path: every answer while the webhook is handled
    session = stack.session_for(laura)
    assert session["status"] == "qualified" and session["source"] == "whatsapp" and session["contact"] == LAURA
    assert session["data"]["full_name"] == "Laura Jiménez Ortiz" and session["data"]["city"] == "Madrid"
    assert stack.candidate(session["id"])["pipeline_stage"] == "qualified"  # lands in «Pre-calificado»


def test_whatsapp_id_check_decision_is_pushed_when_the_registry_answers(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer", verification_mock_delay_seconds=0.5)
    carlos = stack.sim.phone(CARLOS)

    last = whatsapp_screening(carlos, construction(SLOW_ID))[-1][0]  # slow registry: still checking at the end
    session = stack.session_for(carlos)
    assert session["status"] == "verifying", last

    # match the push itself, not "whatever arrives next": it may already be there
    decision = carlos.wait_for_whatsapp(lambda m: m.pushed, timeout=10)
    assert "cumples los requisitos" in decision.body.lower()
    assert stack.wait_for_status(session["id"])["status"] == "qualified"


def test_whatsapp_registry_knockout_arrives_mid_conversation(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer")
    carlos = stack.sim.phone(CARLOS)
    answers = construction(UNKNOWN_ID)
    whatsapp_screening(carlos, answers[:5])  # ... up to the ID number

    knockout = carlos.wait_for_whatsapp(lambda m: m.pushed, timeout=10)  # pushed while the candidate is typing
    assert "•" not in knockout.body
    session = stack.wait_for_status(stack.session_for(carlos)["id"])
    assert session["status"] == "disqualified"
    assert "•" in (session["data"].get("id_document") or "•")  # the ID never travels in clear


def test_slow_turn_is_answered_through_the_api(channel_stack, wa_provider):
    """Twilio gives up on webhooks after 15s (Meta retries): a slow LLM turn must be pushed instead."""
    stack = channel_stack(provider=wa_provider, whatsapp_sync_timeout=0)
    laura = stack.sim.phone(LAURA)

    assert laura.whatsapp("hola") == []  # empty <Response/>: nothing in the webhook answer
    greeting = laura.wait_for_whatsapp("Grupo Sazón")
    assert greeting.pushed
    mark = len(laura.thread)
    assert laura.whatsapp(COURIER[0]) == []
    assert "nombre" in laura.wait_for_whatsapp(since=mark).body.lower()


# ======================= Voice =======================

def test_voice_courier_screening_end_to_end(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider)
    call = stack.sim.phone(LAURA).call()

    assert call.waiting and call.gather_language == "es-ES" and "Grupo Sazón" in call.prompt
    for text in COURIER:
        call.say(text)
    assert not call.active and call.end_reason == "hangup-by-app"  # the agent says goodbye and hangs up
    assert "entrevista" in call.agent_lines()[-1].lower()

    session = stack.session_for(LAURA)
    assert session["status"] == "qualified" and session["source"] == "voice"
    assert "voice_call_ended" in events(stack, session["id"])  # Twilio's status callback was recorded


def test_voice_follows_the_candidate_into_english(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider)
    call = stack.sim.phone(LAURA).call()
    call.say("Hi, I'd like to apply, I only speak English")
    assert call.gather_language == "en-US" and call.prompt_language == "en-US"  # STT and TTS switch together
    for text in COURIER_EN[1:]:
        call.say(text)
    assert stack.session_for(LAURA)["status"] == "qualified"


def test_voice_silence_reprompts_then_says_goodbye(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider)
    call = stack.sim.phone(LAURA).call()
    call.say(COURIER[0])
    question = call.prompt

    for _ in range(settings.voice_max_silence_retries):
        assert question in call.silence()  # re-prompt: nudge + the same question
    call.silence()

    assert call.end_reason == "hangup-by-app"
    session = stack.session_for(LAURA)
    assert session["status"] == "abandoned" and "silence" in session["flags"]
    goodbye = call.agent_lines()[-1].lower()
    assert "volver a llamar" in goodbye, goodbye  # the caller is told they can call back, not "a recruiter will call"
    assert "reclutador" not in goodbye


def test_dropped_call_resumes_at_the_pending_question_on_call_back(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider)
    phone = stack.sim.phone(LAURA)
    call = phone.call()
    for text in COURIER[:3]:
        call.say(text)
    pending = call.prompt
    call.hangup()

    again = phone.call()
    assert pending in again.prompt and "de nuevo" in again.prompt.lower()
    for text in COURIER[3:]:
        again.say(text)
    assert [s["status"] for s in stack.sessions_for(phone)] == ["qualified"]  # one candidate, not two


def test_call_back_after_the_resume_window_starts_over(channel_stack, voice_provider):
    stack = channel_stack(voice=voice_provider)
    phone = stack.sim.phone(LAURA)
    call = phone.call()
    call.say(COURIER[0])
    for _ in range(settings.voice_max_silence_retries + 1):
        call.silence()
    first = stack.session_for(phone)["id"]
    stack.age(first, hours=settings.resume_window_hours + 1)

    assert "Grupo Sazón" in phone.call().prompt  # fresh opening
    assert len(stack.sessions_for(phone)) == 2


# ======================= Pushes, nudges and Twilio faults =======================

def test_silent_whatsapp_candidate_is_nudged_through_the_api(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, COURIER[:2])
    session_id = stack.session_for(laura)["id"]

    mark = len(laura.thread)
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    nudge = laura.wait_for_whatsapp(since=mark)
    assert nudge.pushed and "sigues" in nudge.body.lower()
    assert stack.session(session_id)["events"][-1]["payload"]["pushed"] is True


def test_nudge_outside_the_24h_window_falls_back_to_the_template(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **TEMPLATE_SETTINGS[wa_provider])
    install_template(stack)
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, COURIER[:2])
    session_id = stack.session_for(laura)["id"]

    laura.close_window()  # the app does not know: it tries free-form first
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    nudge = laura.wait_for_whatsapp(lambda m: m.via == "template")
    assert nudge.body == "Hola Laura, soy Marisol. ¿Seguimos con tu candidatura?"
    stack.sim.flush()
    assert "whatsapp_template_fallback" in events(stack, session_id)
    if wa_provider == "twilio":  # Twilio refuses free-form at once...
        rejected = [e for e in stack.sim.log if e.kind == "rest" and e.status == 400]
        assert rejected and "63016" in rejected[0].error
    else:  # ... Meta accepts it and reports 131047 in a status webhook
        failed = [m for m in laura.received(delivered_only=False) if not m.delivered]
        assert [m.error_code for m in failed] == [131047] and "sigues" in failed[0].body.lower()


def test_template_only_sender_still_reaches_the_candidate(channel_stack):
    stack = channel_stack(twilio_whatsapp_content_sid="HXreengage")
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, COURIER[:2])
    stack.sim.faults.template_only = True
    stack.silence(stack.session_for(laura)["id"], seconds=settings.reengage_first_delay + 1)
    assert laura.wait_for_whatsapp(lambda m: m.via == "template").content_sid == "HXreengage"


def test_messages_queued_while_the_provider_is_down_reach_the_candidate_on_their_next_message(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, COURIER[:2])
    session_id = stack.session_for(laura)["id"]

    stack.sim.faults.rest_down = True
    sent = stack.api.post(f"/api/bo/candidates/{session_id}/messages",
                          json={"text": "Hola Laura, soy Ana de RRHH. ¿Tienes un minuto?"}).json()
    assert sent["delivered"] is False
    stack.sim.faults.rest_down = False

    replies = laura.whatsapp("perdona, estaba ocupada")
    assert any("soy Ana" in r for r in replies), replies  # the queued recruiter message is not lost
    transcript = stack.session(session_id)["messages"]
    assert all(m["delivered"] for m in transcript if m["role"] != "user")


# ======================= Recruiter takeover =======================

def test_recruiter_takes_over_a_whatsapp_conversation_and_hands_it_back(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer")
    carlos = stack.sim.phone(CARLOS)
    whatsapp_screening(carlos, construction()[:2])  # at the age question
    session_id = stack.session_for(carlos)["id"]

    stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "Hola Carlos, soy Ana. ¿Hablamos?"})
    pushed = carlos.wait_for_whatsapp("soy Ana")
    assert pushed.pushed
    assert carlos.whatsapp("claro, dime") == []  # the assistant stays silent during the takeover

    mark = len(carlos.thread)
    stack.api.post(f"/api/bo/candidates/{session_id}/takeover", json={"paused": False})
    assert "18" in carlos.wait_for_whatsapp(since=mark).body  # hand-back re-asks the pending question
    assert "permiso" in carlos.whatsapp("sí, tengo 34 años")[0].lower()
    types = events(stack, session_id)
    assert {"takeover_start", "candidate_message_during_takeover", "takeover_end"} <= set(types)


# ======================= Channel switching =======================

def test_whatsapp_to_voice_continues_the_same_screening(channel_stack, wa_provider, voice_provider):
    stack = channel_stack(provider=wa_provider, voice=voice_provider)
    laura = stack.sim.phone(LAURA)
    replies = whatsapp_screening(laura, COURIER[:4])  # name, licence, city answered on WhatsApp
    pending = replies[-1][0]

    call = laura.call()
    assert "Grupo Sazón" not in call.prompt  # no second opening: it is the same screening
    assert "de nuevo" in call.prompt.lower() and pending in call.prompt
    for text in COURIER[4:]:
        call.say(text)

    sessions = stack.sessions_for(laura)
    assert len(sessions) == 1, [s["source"] for s in sessions]
    session = stack.session(sessions[0]["id"])
    assert session["status"] == "qualified" and session["source"] == "whatsapp"  # source = first channel
    assert session["channels"] == ["whatsapp", "voice"]
    switch = next(e for e in session["events"] if e["type"] == "channel_switch")
    assert switch["payload"] == {"from": "whatsapp", "to": "voice", "stage": "availability"}
    assert session["data"]["city"] == "Madrid" and session["data"]["availability"] == ["full_time"]


def test_voice_to_whatsapp_after_a_dropped_call(channel_stack, wa_provider, voice_provider):
    stack = channel_stack(provider=wa_provider, voice=voice_provider)
    laura = stack.sim.phone(LAURA)
    call = laura.call()
    for text in COURIER[:3]:
        call.say(text)
    pending = call.prompt
    call.hangup()

    greeting = laura.whatsapp("hola, se me cortó la llamada")
    assert len(greeting) == 1 and pending in greeting[0] and "de nuevo" in greeting[0].lower()
    for text in COURIER[3:]:
        laura.whatsapp(text)
    [session] = stack.sessions_for(laura)
    assert session["status"] == "qualified" and session["source"] == "voice"
    assert stack.session(session["id"])["channels"] == ["voice", "whatsapp"]


def test_back_and_forth_between_channels(channel_stack, wa_provider, voice_provider):
    stack = channel_stack(provider=wa_provider, voice=voice_provider, whatsapp_profile="construction_laborer", voice_profile="construction_laborer")
    carlos = stack.sim.phone(CARLOS)
    answers = construction()
    whatsapp_screening(carlos, answers[:4])

    call = carlos.call()
    for text in answers[4:7]:  # ID number, area and physical work by phone
        call.say(text)
    call.hangup()

    back = carlos.whatsapp("ya estoy por aquí")
    assert "curso de prl" in back[0].lower()  # WhatsApp picks up at the question the call stopped at
    for text in answers[7:]:
        carlos.whatsapp(text)

    [row] = stack.sessions_for(carlos)
    session = stack.wait_for_status(row["id"])
    assert session["status"] == "qualified" and session["data"]["id_check"] == "valid"
    switches = [e["payload"] for e in session["events"] if e["type"] == "channel_switch"]
    assert [(s["from"], s["to"]) for s in switches] == [("whatsapp", "voice"), ("voice", "whatsapp")]
    user_lines = [m["content"] for m in session["messages"] if m["role"] == "user"]
    assert "Getafe." in user_lines and "tengo coche" in user_lines  # one transcript across both channels (STT punctuates)


def test_recruiter_messages_reach_whatsapp_while_the_candidate_is_on_the_phone(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, COURIER[:2])
    call = laura.call()
    call.say(COURIER[2])
    session_id = stack.session_for(laura)["id"]

    result = stack.api.post(f"/api/bo/candidates/{session_id}/messages",
                            json={"text": "Laura, te escribo por aquí el enlace del contrato", "pause_bot": False}).json()
    assert result["delivered"] is True
    assert laura.wait_for_whatsapp("enlace del contrato").pushed


def test_whatsapp_becomes_the_push_channel_after_switching_from_voice(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    call = laura.call()
    call.say(COURIER[0])
    call.hangup()
    laura.whatsapp("hola")
    session_id = stack.session_for(laura)["id"]

    mark = len(laura.thread)
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    assert laura.wait_for_whatsapp(since=mark).pushed  # the nudge is pushed, not left in a queue


def test_candidate_registered_by_a_recruiter_continues_on_whatsapp(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer")
    created = stack.api.post("/api/bo/candidates", json={
        "profile_id": "construction_laborer", "name": "Carlos Martín Ruiz", "contact": "+34 600 333 444",
        "note": "Referido por el encargado de Getafe"}).json()

    opening = stack.sim.phone(CARLOS).whatsapp("hola, me ha pasado vuestro número el encargado")
    assert len(opening) == 1 and "Constructora Horizonte" in opening[0]  # the opening, not "welcome back"
    [row] = stack.sessions_for(CARLOS)
    assert row["id"] == created["id"]  # the recruiter's candidate, not a duplicate
    assert stack.session(created["id"])["channels"] == ["backoffice", "whatsapp"]


def test_a_final_decision_is_not_reopened_from_another_channel(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    whatsapp_screening(laura, [COURIER[0], COURIER[1], "no tengo carnet"])
    assert stack.session_for(laura)["status"] == "disqualified"

    assert "Grupo Sazón" in laura.call().prompt  # a new call is a new conversation
    assert sorted(s["source"] for s in stack.sessions_for(laura)) == ["voice", "whatsapp"]


def test_channels_bound_to_different_processes_keep_separate_screenings(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer", voice_profile="delivery_courier")
    carlos = stack.sim.phone(CARLOS)
    whatsapp_screening(carlos, construction()[:3])

    assert "Grupo Sazón" in carlos.call().prompt  # the voice line runs the courier process
    profiles = sorted(s["profile_id"] for s in stack.sessions_for(carlos))
    assert profiles == ["construction_laborer", "delivery_courier"]


def test_switching_to_whatsapp_during_a_takeover_keeps_the_assistant_silent(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    call = laura.call()
    call.say(COURIER[0])
    call.hangup()
    session_id = stack.session_for(laura)["id"]
    stack.api.post(f"/api/bo/candidates/{session_id}/takeover", json={"paused": True})

    assert laura.whatsapp("hola, ¿me llamáis vosotros?") == []
    messages = stack.session(session_id)["messages"]
    assert messages[-1]["role"] == "user" and "me llamáis" in messages[-1]["content"]


# ======================= Security =======================

def test_webhooks_signed_with_another_token_are_rejected(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    stack.sim.twilio.auth_token = "not-the-apps-token"  # a forger who knows neither TWILIO_AUTH_TOKEN ...
    stack.sim.meta.app_secret = "not-the-apps-secret"  # ... nor META_APP_SECRET
    assert stack.sim.phone(LAURA).whatsapp("hola") == []
    assert stack.sim.log[-1].status == 403
    assert stack.sessions_for(LAURA) == []
