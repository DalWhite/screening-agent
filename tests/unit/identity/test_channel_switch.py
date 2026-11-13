"""Identity analysis §2A: the same candidate reaches us on another channel.

Already covered (and passing), not repeated here:

* WhatsApp <-> phone, same number and process, screening undecided:
  ``tests/integration/channels/test_stories.py`` (``test_whatsapp_to_voice_continues_the_same_screening``,
  ``test_voice_to_whatsapp_after_a_dropped_call``, ``test_back_and_forth_between_channels``).
* Recruiter referral with ``+country`` -> WhatsApp / web chat: ``test_candidate_registered_by_a_recruiter_continues_on_whatsapp``,
  ``tests/unit/channels/test_web_channel.py::test_a_referral_opening_the_screening_link_switches_to_the_web``.

Covered in other files of this suite: a web / ATS session that knows the phone number
(``tests/integration/identity``), referrals typed without country code (``test_edge_cases.py``),
both channels at the same moment (``test_concurrency.py``).
"""

from __future__ import annotations

from tests.support.identity import is_opening, place_call, send_whatsapp, sessions_for

# A Mexican mobile as WhatsApp identifies it (wa_id keeps the legacy "1" after +52) and as the
# same line shows up in caller ID (E.164 since the 2019 dialling reform).
MX_WHATSAPP = "+5215512345678"
MX_CALLER_ID = "+525512345678"


def test_a_mexican_candidate_who_wrote_on_whatsapp_continues_on_the_phone(whatsapp, voice):
    send_whatsapp(whatsapp, MX_WHATSAPP, "hola", "sí, empecemos", "José Pérez Luna")

    action = voice.start_call(MX_CALLER_ID, "CA-mx-1")

    assert not is_opening(action.text), f"the call started a second screening: {action.text!r}"
    assert len(sessions_for(MX_WHATSAPP, MX_CALLER_ID)) == 1


def test_a_mexican_candidate_who_called_first_continues_on_whatsapp(whatsapp, voice):
    place_call(voice, MX_CALLER_ID, "CA-mx-2", "sí, empecemos", "José Pérez Luna")

    replies = send_whatsapp(whatsapp, MX_WHATSAPP, "hola, se me cortó la llamada")

    assert replies and not is_opening(replies[0]), f"WhatsApp started a second screening: {replies!r}"
    assert len(sessions_for(MX_WHATSAPP, MX_CALLER_ID)) == 1
