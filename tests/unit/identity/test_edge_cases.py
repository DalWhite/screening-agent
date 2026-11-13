"""Identity analysis §3: edge cases of identifying a person by phone number.

E1 withheld caller IDs · E2 a number shared by two people (guard) · E3 the ID document as
identity (+ privacy guard) · E4 referrals (duplicates, number formats). E6 (retention) is an
app-level test in ``tests/integration/identity``. Not covered on purpose, they need a product
decision first: E5 (WhatsApp usernames / business-scoped user ids instead of phone numbers) and
E7 (several browser tabs polling the same chat).

Already covered: referral numbers with ``+country`` and spaces/dashes are normalised
(``tests/unit/channels/test_continuity.py::test_recruiter_contacts_are_stored_like_channels_address_them``,
extended there with the ``00`` international prefix).
"""

from __future__ import annotations

import hashlib

import pytest

from backend.backoffice.service import Invalid
from backend.storage import db
from tests.support.identity import (assert_same_candidate, construction, courier, courier_no_license, is_opening,
                                    send_whatsapp, sessions_for, text_cells)

# What telephony providers put in the caller ID when the number is withheld. Twilio documents its
# placeholders (+266696687 ANONYMOUS, +7378742833 RESTRICTED, +2562533 BLOCKED, +8656696 UNAVAILABLE,
# +86282452253 UNKNOWN) besides the literal "anonymous"; carriers also pass words through.
WITHHELD_CALLER_IDS = ["anonymous", "Restricted", "unknown", "+266696687", "+7378742833", "+2562533", "+8656696",
                       "+86282452253"]


# ---------- E1: withheld numbers are nobody's identity ----------

@pytest.mark.parametrize("caller_id", WITHHELD_CALLER_IDS)
def test_two_callers_with_a_withheld_number_are_two_candidates(voice, caller_id):
    first = voice.start_call(caller_id, "CA-first")
    voice.speech(first.session_id, "sí, empecemos", call_id="CA-first")
    voice.speech(first.session_id, "Ana Torres Gil", call_id="CA-first")

    second = voice.start_call(caller_id, "CA-second")  # someone else, also with a hidden number

    assert second.session_id != first.session_id, "a stranger continued another caller's screening"
    assert is_opening(second.text) and "Ana" not in second.text
    assert db.get_session(second.session_id)["data"].get("full_name") in (None, "")


# ---------- E2: a number shared by two people (guard: passes today, must keep passing) ----------

def test_another_person_on_the_same_number_never_overwrites_the_first_ones_screening(whatsapp):
    """A family phone: Laura was qualified, her sister Marta writes from the same number. However
    identity ends up linking numbers to people, Laura's screening must stay Laura's."""
    send_whatsapp(whatsapp, "+34600111222", "hola", *courier("Laura Jiménez Ortiz"))
    [laura] = sessions_for("+34600111222")

    send_whatsapp(whatsapp, "+34600111222", "hola, soy Marta, la hermana de Laura, quiero aplicar yo",
                  *courier_no_license("Marta Jiménez Ortiz"))

    session = db.get_session(laura["id"])
    assert session["status"] == "qualified" and session["data"]["full_name"] == "Laura Jiménez Ortiz"
    assert session["data"]["has_license"] is True


# ---------- E3: the ID document ----------

def test_the_same_id_document_from_two_numbers_is_the_same_candidate(lines):
    """A new phone (or a fraud attempt): the identity document is the strongest key we collect."""
    line = lines("whatsapp", "construction_laborer")
    answers = construction("Carlos Martín Ruiz", "12345678Z")[:5]  # ... up to the ID number
    send_whatsapp(line, "+34600333444", "hola", *answers)
    send_whatsapp(line, "+34611999888", "hola", *answers)

    rows = sessions_for("+34600333444", "+34611999888")
    assert len(rows) == 2
    assert all(db.get_session(r["id"])["data"].get("id_document") for r in rows)  # both gave the document
    assert_same_candidate(*[r["id"] for r in rows])


def test_no_id_number_nor_a_plain_hash_of_it_is_stored(lines):
    """Guard for whatever identity key the design derives from the document: never the number in
    clear, never an unsalted hash (10^8 DNI numbers: a dictionary attack takes seconds). The pending
    verification payload is the one place allowed to hold it until the check answers."""
    send_whatsapp(lines("whatsapp", "construction_laborer"), "+34600333444", "hola",
                  *construction("Carlos Martín Ruiz", "12345678Z"))
    [row] = sessions_for("+34600333444")
    assert db.get_session(row["id"])["data"]["id_document"].endswith("678Z")  # the number was collected (masked)

    stored = text_cells(exclude={("verifications", "payload_json")})
    assert "12345678Z" not in stored.upper()
    everything = text_cells()
    for value in ("12345678Z", "12345678z", "12345678"):
        for algorithm in ("md5", "sha1", "sha256", "sha512"):
            digest = hashlib.new(algorithm, value.encode()).hexdigest()
            assert digest not in everything, f"unsalted {algorithm} of the ID number is stored"


# ---------- E4: referrals ----------

def test_registering_the_same_referral_twice_gives_one_candidate(bo):
    first = bo.create_candidate("delivery_courier", by="Ana", name="Luis Mora Vega", contact="+34 600 555 666")
    try:
        second = bo.create_candidate("delivery_courier", by="Eva", name="Luis Mora Vega", contact="+34600555666")
    except Invalid:
        return  # refused as a duplicate: fine
    assert second["id"] == first["id"]
    assert len(sessions_for("+34600555666")) == 1


def test_a_referral_typed_without_country_code_is_recognised_or_refused(bo, whatsapp):
    """Recruiters type numbers as people say them ("600 555 666"). Stored as typed, WhatsApp (always
    +34...) never finds the referral and opens a second screening. Either normalise it (Spain is the
    main market) or refuse it and ask for the country code."""
    try:
        created = bo.create_candidate("delivery_courier", by="Ana", name="Luis Mora Vega", contact="600 555 666")
    except Invalid:
        return

    send_whatsapp(whatsapp, "+34600555666", "hola, me ha dado vuestro número Ana")

    rows = sessions_for("+34600555666", "600555666")
    assert [r["id"] for r in rows] == [created["id"]], f"the referral and the WhatsApp candidate are apart: {rows}"
