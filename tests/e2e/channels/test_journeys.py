"""Candidate journeys across channels, end to end over real HTTP.

Every journey runs on both WhatsApp providers (``live``: Twilio and Meta's
Cloud API) unless it is phone-only, ends with the line-for-line check that the
backend transcript equals what the candidate lived (``assert_nothing_lost``),
and is looked at through the recruiter's eyes in the backoffice.
"""

from __future__ import annotations

from urllib.parse import quote

from playwright.sync_api import Page, expect

from simulators import speech
from tests.support.candidates import SLOW_ID, construction_answers, unique_phone, unique_surname
from tests.support.channels import CONSTRUCTION, assert_nothing_lost, only_session, open_candidate


def test_whatsapp_screening_lands_in_the_pipeline(page: Page, live):
    name = f"Carlos {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    answers = construction_answers(name, SLOW_ID)  # the registry answers after the last question
    phone.whatsapp("hola, vi la oferta de peón")
    replies = [phone.whatsapp(text) for text in answers]
    assert all(len(r) == 1 for r in replies)
    assert "verificando" in replies[-1][0].lower()

    decision = phone.wait_for_whatsapp(lambda m: m.pushed, timeout=20)  # pushed when the registry answers
    assert "cumples los requisitos" in decision.body.lower()
    session = assert_nothing_lost(live, phone)
    assert session["status"] == "qualified" and session["data"]["id_check"] == "valid"

    page.goto(f"{live.url}/backoffice#/pipeline?q={quote(name)}")
    card = page.get_by_test_id("column-qualified").get_by_test_id("candidate-card").filter(has_text=name)
    expect(card).to_be_visible()
    card.click()  # quick view: the conversation is there, with the pushed decision
    expect(page.get_by_role("dialog")).to_contain_text("cumples los requisitos")


def test_phone_only_screening_with_realistic_speech_recognition(page: Page, live_voice):
    """A plainer STT engine: no casing, no punctuation, the ID number spelled digit by digit."""
    live_voice.sim.transcriber = speech.chain(speech.digit_by_digit, speech.plain_lowercase)
    name = f"Mario {unique_surname()}"
    phone = live_voice.sim.phone(unique_phone(), name)
    call = phone.call()
    for text in construction_answers(name):
        call.say(text, confidence=0.83)
    assert not call.active and call.end_reason == "hangup-by-app"
    session = live_voice.wait_for_status(only_session(live_voice, phone)["id"])
    assert session["status"] == "qualified" and session["data"]["id_document"].startswith("•")
    assert session["data"]["full_name"] == name  # "mario xxx" from the STT, stored as a name
    assert session["source"] == "voice" and session["channels"] == ["voice"]

    open_candidate(page, live_voice, session["id"])
    spoken = page.get_by_test_id("transcript").locator(".msg.kind-speech")
    expect(spoken).to_have_count(len(construction_answers(name)))
    expect(spoken.first.get_by_test_id("msg-kind")).to_contain_text("confianza 83%")


def test_whatsapp_phone_whatsapp_with_voice_notes_photos_and_silence(page: Page, live):
    name = f"Rosa {unique_surname()}"
    answers = construction_answers(name)
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp(answers[0])
    phone.voice_note(answers[1])  # name by voice note
    phone.photo()  # a photo of their ID, declined and never stored
    phone.whatsapp(answers[2])
    call = phone.call()
    call.say(answers[3])
    call.silence()  # thinking...
    call.say(answers[4])
    call.hangup()
    assert "físico" in phone.whatsapp(answers[5])[-1]  # back on WhatsApp with the answer to the pending question
    for text in answers[6:]:
        phone.voice_note(text)

    session = assert_nothing_lost(live, phone)
    assert live.wait_for_status(session["id"])["status"] == "qualified"
    assert session["channels"] == ["whatsapp", "voice"]

    open_candidate(page, live, session["id"])
    transcript = page.get_by_test_id("transcript")
    expect(transcript.get_by_test_id("channel-divider")).to_have_text(["WhatsApp", "Teléfono", "WhatsApp"])
    expect(transcript.locator(".msg.kind-voice_note")).to_have_count(1 + len(answers[6:]))
    expect(transcript.locator(".msg.kind-reprompt")).to_contain_text("¿Sigues ahí?")
    expect(transcript.locator(".msg.kind-media")).to_contain_text("[Foto]")


def test_english_candidate_keeps_english_across_channels(page: Page, live):
    name = f"John {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hi")
    assert "name" in phone.whatsapp("Hi, I only speak English. Yes, let's start")[-1].lower()
    phone.whatsapp(name)
    call = phone.call()
    assert call.gather_language == "en-US" and call.prompt_language == "en-US"
    assert "welcome back" in call.prompt.lower()
    call.say("yes, I am 30 years old")
    call.hangup()
    assert "work" in phone.whatsapp("hello again")[-1].lower()  # the pending question, in English
    session = assert_nothing_lost(live, phone)
    assert session["language"] == "en"

    open_candidate(page, live, session["id"], tab="profile")
    expect(page.locator(".info-list")).to_contain_text("en")


def test_each_line_runs_its_own_process(stacks):
    stack = stacks.get("twilio", WHATSAPP_PROFILE="construction_laborer", VOICE_PROFILE="delivery_courier")
    phone = stack.sim.phone(unique_phone())
    assert "Constructora Horizonte" in phone.whatsapp("hola")[0]
    assert "Grupo Sazón" in phone.call().prompt  # the voice line recruits couriers: a separate screening
    assert sorted(s["profile_id"] for s in stack.sessions_for(phone)) == ["construction_laborer", "delivery_courier"]


def test_a_rejected_candidate_who_calls_again_is_not_screened_again(page: Page, live):
    """A decision is final and the candidate stays one (docs/identity_design.md): within the follow-up
    period the call takes a message for the recruiter instead of starting a second screening."""
    name = f"Lucía {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    phone.whatsapp(name)
    assert "18" in phone.whatsapp("tengo 17 años")[0] or live.sessions_for(phone)[0]["status"] == "disqualified"
    assert live.sessions_for(phone)[0]["status"] == "disqualified"
    call = phone.call()
    assert "Constructora Horizonte" not in call.prompt and "ya tenemos tu candidatura" in call.prompt.lower()
    call.hangup()
    assert [s["status"] for s in live.sessions_for(phone)] == ["disqualified"]  # not reopened, no second one

    page.goto(f"{live.url}/backoffice#/candidates?q={quote(phone.number.lstrip('+'))}")
    expect(page.get_by_test_id("candidate-row")).to_have_count(1)


def test_silent_caller_is_closed_then_resumes_on_whatsapp(page: Page, live):
    name = f"Pedro {unique_surname()}"
    phone = live.sim.phone(unique_phone(), name)
    call = phone.call()
    call.say("sí")
    assert "nombre" in call.prompt.lower()
    for _ in range(3):
        call.silence()
    assert "volver a llamar" in call.agent_lines()[-1]
    session_id = only_session(live, phone)["id"]
    assert live.session(session_id)["status"] == "abandoned"

    replies = phone.whatsapp("hola, perdona, no podía hablar")  # small talk with a "no" in it: not an answer
    assert "de nuevo" in replies[0].lower() and "nombre" in replies[-1].lower()  # resumed at the same question
    phone.whatsapp(name)
    session = assert_nothing_lost(live, phone)
    assert session["status"] == "active" and session["data"]["full_name"] == name

    open_candidate(page, live, session_id, tab="activity")
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("Screening cerrado: abandoned")
    expect(timeline).to_contain_text("El candidato retomó la conversación")
    expect(timeline).to_contain_text("El candidato cambió de canal: Teléfono → WhatsApp")


def test_referral_with_a_spaced_phone_number_starts_on_the_phone(page: Page, live_voice):
    """A recruiter registers a referral; the candidate calls instead of opening the link."""
    name = f"Jorge {unique_surname()}"
    number = unique_phone()
    created = live_voice.api.post("/api/bo/candidates", json={
        "profile_id": "construction_laborer", "name": name,
        "contact": f"{number[:3]} {number[3:6]} {number[6:9]} {number[9:]}"}).json()
    call = live_voice.sim.phone(number).call()
    assert "Constructora Horizonte" in call.prompt  # the opening: they never talked to us before
    call.say("sí")
    assert "18" in call.prompt  # the name came from the recruiter: straight to the age
    call.hangup()
    session = only_session(live_voice, number)
    assert session["id"] == created["id"] and session["channels"] == ["backoffice", "voice"]

    open_candidate(page, live_voice, created["id"])
    expect(page.locator(".cand-sub")).to_contain_text("Alta manual → Teléfono")


def test_channel_filter_is_the_channel_where_the_screening_started(page: Page, channels):
    """The list's *Canal* filter (like analytics) segments by origin: a WhatsApp candidate who then
    called is under WhatsApp, not Teléfono — the file shows the whole route."""
    tag = unique_surname()
    phone = channels.sim.phone(unique_phone())
    phone.whatsapp("hola")
    phone.whatsapp("sí")
    phone.whatsapp(f"Sara {tag}")
    phone.call().hangup()

    page.goto(f"{channels.url}/backoffice#/candidates?q={tag}&source=whatsapp")
    expect(page.get_by_test_id("candidate-row")).to_have_count(1)
    page.goto(f"{channels.url}/backoffice#/candidates?q={tag}&source=voice")
    expect(page.get_by_test_id("result-count")).to_contain_text("0")


def test_construction_process_on_both_lines_by_default(stacks):
    stack = stacks.get("meta", **CONSTRUCTION)
    assert "Constructora Horizonte" in stack.sim.phone(unique_phone()).whatsapp("hola")[0]


def test_a_stack_without_twilio_at_all(page: Page, stacks):
    """WhatsApp on Meta's Cloud API, the phone line on Vonage: the same screening, nothing else to change."""
    stack = stacks.get("meta", voice="vonage", **CONSTRUCTION)
    name = f"Noa {unique_surname()}"
    answers = construction_answers(name)
    phone = stack.sim.phone(unique_phone(), name)
    phone.whatsapp("hola")
    phone.voice_note(answers[0])
    phone.whatsapp(answers[1])
    call = phone.call()
    assert call.provider == "vonage" and "de nuevo" in call.prompt.lower()
    for text in answers[2:6]:
        call.say(text)
    call.silence()
    call.hangup()
    for text in answers[6:]:
        phone.whatsapp(text)
    session = assert_nothing_lost(stack, phone)
    assert stack.wait_for_status(session["id"])["status"] == "qualified"
    assert not [e for e in stack.sim.log if "2010-04-01" in e.url or "/voice/inbound" in e.url]  # no Twilio traffic

    open_candidate(page, stack, session["id"])
    expect(page.get_by_test_id("transcript").get_by_test_id("channel-divider")).to_have_text(
        ["WhatsApp", "Teléfono", "WhatsApp"])
    expect(page.get_by_text("Se envía por WhatsApp.")).to_be_visible()
