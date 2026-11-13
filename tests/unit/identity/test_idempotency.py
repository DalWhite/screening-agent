"""Identity analysis §4: the same request twice (provider retries, client retries) is one request.

Already covered (and passing), not repeated here:

* a WhatsApp retry with the same message id, one process, one after the other:
  ``tests/integration/channels/test_conversation_registry.py::test_webhook_retries_are_processed_once``;
* claims survive a restart / a failed turn lets the retry through / a crashed claim is taken over:
  ``tests/integration/channels/test_robustness.py``;
* delivery receipts only move forward, also concurrently: ``test_concurrent_receipts_only_move_a_delivery_forward``.

HTTP-level idempotency (``Idempotency-Key`` on the session API, replayed voice webhooks) is in
``tests/integration/identity``.
"""

from __future__ import annotations

from backend.channels.contracts import InboundMessage
from backend.channels.messaging import MessagingChannel
from backend.config import settings
from backend.session_store import SessionStore
from backend.storage import db
from tests.support.identity import (DEFERRED_MULTI_WORKER, at_once, event_types, send_whatsapp, sessions_for,
                                    user_lines, with_db_latency)

ROUNDS = 10


# ---------- WhatsApp provider retries ----------

@DEFERRED_MULTI_WORKER
def test_a_provider_message_is_claimed_by_exactly_one_worker(temp_db, monkeypatch):
    """The database claim is the de-duplication shared by every worker: check-then-insert lets two
    concurrent deliveries of one message both through."""
    with_db_latency(monkeypatch)
    doubles = [i for i in range(ROUNDS)
               if at_once(*[lambda i=i: db.claim_inbound("meta", f"wamid.race{i}")] * 3).count(True) != 1]
    assert not doubles, f"{len(doubles)}/{ROUNDS} messages were claimed by several workers (or none)"


@DEFERRED_MULTI_WORKER
def test_a_claim_is_not_taken_over_while_its_turn_can_still_be_running():
    """A claim still "processing" is handed to a provider retry after INBOUND_STALE_SECONDS. A turn can
    legitimately take longer: voice-note transcription plus two model calls (extraction + phrasing)."""
    longest_turn = settings.stt_timeout + 2 * settings.llm_timeout
    assert db.INBOUND_STALE_SECONDS > longest_turn, (
        f"a retry can re-run a turn that is still running: claims go stale after {db.INBOUND_STALE_SECONDS}s, "
        f"a turn may take {longest_turn:.0f}s")


@DEFERRED_MULTI_WORKER
def test_a_retry_delivered_to_another_worker_at_the_same_time_is_processed_once(agent, provider, temp_db, monkeypatch):
    """Two workers (own in-memory state each) get the same provider message at once."""
    with_db_latency(monkeypatch)
    twice = []
    for i in range(ROUNDS):
        number = f"+3460070{i:04d}"
        first, second = (MessagingChannel(agent, SessionStore(), provider, profile_id="delivery_courier")
                         for _ in range(2))
        send_whatsapp(first, number, "hola")
        message = InboundMessage(contact=f"whatsapp:{number}", text="sí, empecemos", message_id=f"SMretry{i}",
                                 provider="twilio")
        at_once(lambda: first.process_message(message), lambda: second.process_message(message))
        lines = [line for row in sessions_for(number) for line in user_lines(row["id"])]
        if lines.count("sí, empecemos") != 1:
            twice.append(number)
    assert not twice, f"{len(twice)}/{ROUNDS} retried messages were processed twice"


# ---------- phone provider retries (same call id) ----------

def test_a_retried_call_start_is_the_same_call(voice):
    """The provider re-posts the incoming-call webhook (timeout, fallback URL): same call, same answer."""
    first = voice.start_call("+34600111222", "CA-retried")
    retry = voice.start_call("+34600111222", "CA-retried")

    assert retry.session_id == first.session_id
    assert retry.text == first.text, "the retry was answered as a call-back ('welcome back')"
    assert event_types(first.session_id).count("voice_call_started") == 1


def test_concurrent_deliveries_of_a_call_start_open_one_screening(voice, monkeypatch):
    with_db_latency(monkeypatch)
    doubles = []
    for i in range(ROUNDS):
        number = f"+3460080{i:04d}"
        at_once(lambda: voice.start_call(number, f"CA-dup{i}"), lambda: voice.start_call(number, f"CA-dup{i}"))
        if len(sessions_for(number)) != 1:
            doubles.append(number)
    assert not doubles, f"{len(doubles)}/{ROUNDS} calls opened two screenings"
