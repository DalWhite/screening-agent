"""WhatsApp voice notes and other media, on both providers.

Voice notes are downloaded from the provider (Twilio MediaUrl with Basic auth,
Meta's two-step media lookup), transcribed through the OpenAI-compatible STT
endpoint (the simulator's), and handled exactly like typed answers. The audio
is never stored; other media (photos, stickers...) are never downloaded.
"""

from __future__ import annotations

from simulators.speech import make_voice_note

LAURA = "+34600111222"
CARLOS = "+34600333444"


def at_licence_question(stack, number=LAURA):
    phone = stack.sim.phone(number)
    for text in ["hola", "sí", "Laura Jiménez Ortiz"]:
        phone.whatsapp(text)
    return phone


def test_a_voice_note_answers_the_pending_question(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    replies = laura.voice_note("sí, tengo el carnet de coche desde hace años")
    assert "ciudad" in replies[0].lower()  # licence accepted, next question
    session = stack.session_for(laura)
    assert session["data"]["has_license"] is True
    note = [m for m in session["messages"] if m["kind"] == "voice_note"][-1]
    assert note["content"] == "sí, tengo el carnet de coche desde hace años"
    assert note["meta"]["transcribed"] and not note["meta"]["audio_stored"] and note["meta"]["duration"] > 0
    downloads = [e for e in stack.sim.log if e.kind == "rest" and e.method == "GET"]
    assert downloads and all(e.status == 200 for e in downloads)  # fetched with the provider's credentials


def test_a_whole_screening_by_voice_notes(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    laura.voice_note("hola, vengo por la oferta de repartidor")
    for text in ["sí, empecemos", "Laura Jiménez Ortiz", "sí, tengo carnet", "Madrid, Malasaña",
                 "jornada completa", "mañana", "2 años en Glovo", "en 2 semanas"]:
        laura.voice_note(text)
    session = stack.session_for(laura)
    assert session["status"] == "qualified"
    assert {m["kind"] for m in session["messages"] if m["role"] == "user"} == {"voice_note"}


def test_an_id_number_in_a_voice_note_is_masked_and_verified(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, whatsapp_profile="construction_laborer")
    carlos = stack.sim.phone(CARLOS)
    for text in ["hola", "sí", "Carlos Martín Ruiz", "sí, tengo 34 años", "sí"]:
        carlos.whatsapp(text)
    carlos.voice_note("mi DNI es el 12345678Z")
    session = stack.session_for(carlos)
    note = [m for m in session["messages"] if m["kind"] == "voice_note"][-1]
    assert "12345678" not in note["content"] and "[id redacted]" in note["content"]
    assert session["data"]["id_document"].startswith("•")
    assert stack.wait_for_status(session["id"], {"active"})["stage"] == "location"


def test_english_voice_notes_switch_the_conversation_language(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = stack.sim.phone(LAURA)
    laura.whatsapp("hola")
    reply = laura.voice_note("Hi, sorry, I only speak English. Yes, let's start", language="en")
    assert "name" in reply[-1].lower()
    assert stack.session_for(laura)["language"] == "en"


def test_a_voice_note_that_cannot_be_transcribed_is_answered_kindly(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    stack.sim.faults.stt_down = True
    [reply] = laura.voice_note("sí, tengo carnet")
    assert reply.startswith("No he podido entender tu nota de voz") and "licencia" in reply
    state = stack.app.store.get(stack.session_for(laura)["id"])
    assert state.stage == "license" and state.attempts_for("license") == 0  # no attempt burnt
    stored = [m for m in stack.session(state.id)["messages"] if m["kind"] == "voice_note"][-1]
    assert stored["content"] == "[Nota de voz sin transcribir]" and stored["meta"]["transcribed"] is False

    stack.sim.faults.stt_down = False
    assert "ciudad" in laura.voice_note("sí, tengo carnet")[0].lower()  # the candidate tries again


def test_real_audio_without_speech_recognised(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    silence = make_voice_note("x")[:44]  # just a WAV header: nothing anyone could transcribe
    assert laura.audio(silence, "audio/wav")[0].startswith("No he podido entender")


def test_voice_notes_without_speech_to_text_configured(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, stt_provider="none")
    laura = at_licence_question(stack)
    assert laura.voice_note("sí")[0].startswith("No he podido entender")
    assert not [e for e in stack.sim.log if "/audio/transcriptions" in e.url]  # not even downloaded


def test_a_voice_note_during_a_takeover_reaches_the_recruiter_only(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    session_id = stack.session_for(laura)["id"]
    stack.api.post(f"/api/bo/candidates/{session_id}/takeover", json={"paused": True})
    assert laura.voice_note("¿me podéis llamar mejor por la tarde?") == []
    last = stack.session(session_id)["messages"][-1]
    assert last["kind"] == "voice_note" and last["content"] == "¿me podéis llamar mejor por la tarde?"


def test_photos_are_declined_and_never_stored(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    [reply] = laura.photo()
    assert "Solo puedo leer mensajes de texto y notas de voz" in reply and "licencia" in reply
    stored = stack.session_for(laura)["messages"][-2]
    assert stored["kind"] == "media" and stored["content"] == "[Foto]" and stored["meta"]["stored"] is False
    assert not [e for e in stack.sim.log if e.kind == "rest" and e.method == "GET"]  # never downloaded


def test_a_photo_caption_is_read_as_the_answer(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura = at_licence_question(stack)
    replies = laura.photo(caption="sí, aquí tienes mi carnet")
    assert "Solo puedo leer" in replies[0] and "ciudad" in replies[-1].lower()
    assert stack.session_for(laura)["data"]["has_license"] is True


def test_a_voice_note_opens_a_new_conversation(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    [opening] = stack.sim.phone(LAURA).voice_note("hola, quería información del trabajo")
    assert "Grupo Sazón" in opening
    first = stack.session_for(LAURA)["messages"][0]
    assert first["role"] == "user" and first["kind"] == "voice_note"
