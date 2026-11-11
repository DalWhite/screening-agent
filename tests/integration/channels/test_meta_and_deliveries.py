"""WhatsApp on Meta's Cloud API (WHATSAPP_PROVIDER=meta) and the delivery registry.

Meta specifics: the webhook subscription handshake, X-Hub-Signature-256, JSON
payloads (text, buttons, media, statuses), replies through the Graph API,
read receipts, Graph errors. The delivery registry and the asynchronous
"outside the 24h window" failure are checked on both providers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.integrations.meta.client import MetaClient
from backend.config import settings
from backend.storage import db

LAURA = "+34600111222"
TEMPLATE = {"twilio": {"twilio_whatsapp_content_sid": "HXreengage"},
            "meta": {"meta_template_name": "reengage_candidates"}}
BODY = "Hola {{1}}, soy {{2}}. ¿Seguimos con tu candidatura?"


def meta_stack(channel_stack, **overrides):
    return channel_stack(provider="meta", **overrides)


def pushes(session_id):
    """Business-initiated deliveries (with Meta, turn replies are Graph API sends too: kind "reply")."""
    return [d for d in db.deliveries(session_id) if d["kind"] != "reply"]


def started(stack, number=LAURA):
    phone = stack.sim.phone(number, "Laura")
    for text in ["hola", "sí", "Laura Jiménez Ortiz"]:
        phone.whatsapp(text)
    return phone, stack.session_for(phone)["id"]


# ---------- webhook security and shape ----------

def test_webhook_subscription_handshake(channel_stack):
    stack = meta_stack(channel_stack)
    status, body = stack.sim.meta.verify_webhook()
    assert status == 200 and body.isdigit()  # echoes hub.challenge
    assert stack.sim.meta.verify_webhook(token="guessed")[0] == 403


def test_unsigned_or_forged_webhooks_are_rejected(channel_stack):
    stack = meta_stack(channel_stack)
    stack.sim.meta.app_secret = "someone-else"
    assert stack.sim.phone(LAURA).whatsapp("hola") == []
    assert stack.sim.log[-1].status == 403 and stack.sessions_for(LAURA) == []


def test_only_the_active_providers_webhooks_exist(channel_stack):
    stack = meta_stack(channel_stack)
    resp = stack.api.post("/api/channels/whatsapp/inbound", data={"From": f"whatsapp:{LAURA}", "Body": "hola"})
    assert resp.status_code in (404, 405)  # the Twilio WhatsApp adapter is not mounted at all
    assert stack.api.get("/api/channels/whatsapp/meta", params={"hub.mode": "subscribe"}).status_code == 403


def test_read_receipts_and_template_quick_replies(channel_stack):
    stack = meta_stack(channel_stack)
    laura, _ = started(stack)
    assert all(m.read for m in laura.thread if m.sender == "candidate")  # blue ticks for the candidate
    # a template's quick-reply button arrives as type "button": it is read like text
    payload = stack.sim.meta._envelope({"messages": [{"from": LAURA[1:], "id": "wamid.button1", "timestamp": "1",
                                                      "type": "button", "button": {"text": "sí", "payload": "yes"}}]})
    stack.sim.meta.post(payload, phone=LAURA)
    assert stack.session_for(laura)["data"]["has_license"] is True


def test_messages_for_another_number_of_the_business_account_are_ignored():
    provider = MetaClient(config=type("C", (), {"meta_phone_number_id": "111", "meta_validate_signatures": False})())
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "222"}, "messages": [{"from": "34600", "id": "x", "type": "text",
                                                              "text": {"body": "hola"}}]}}]}]}
    assert provider.parse_webhook(payload) == ([], [])


def test_payload_types_are_mapped():
    provider = MetaClient(config=type("C", (), {"meta_phone_number_id": "111"})())
    messages = [
        {"from": "34600111222", "id": "a", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": "1", "title": "Sí"}}},
        {"from": "34600111222", "id": "b", "type": "location", "location": {"latitude": 40.3, "longitude": -3.7}},
        {"from": "34600111222", "id": "c", "type": "reaction", "reaction": {"message_id": "x", "emoji": "👍"}},
        {"from": "34600111222", "id": "d", "type": "sticker", "sticker": {"mime_type": "image/webp", "id": "m1"}},
    ]
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "111"}, "messages": messages,
        "statuses": [{"id": "wamid.1", "status": "failed", "recipient_id": "34600111222",
                      "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    parsed, statuses = provider.parse_webhook(payload)
    assert [(m.message_id, m.text, m.kind) for m in parsed] == [("a", "Sí", None), ("b", "", "location"),
                                                                ("d", "", "sticker")]  # reactions need no answer
    assert parsed[2].media[0].media_type == "sticker" and parsed[0].contact == "+34600111222"
    assert (statuses[0].status, statuses[0].error_code, statuses[0].contact) == ("failed", 131047, "+34600111222")


# ---------- delivery registry ----------

def test_every_push_is_tracked_until_read(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider)
    laura, session_id = started(stack)
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    laura.wait_for_whatsapp("sigues")
    stack.sim.flush()
    assert [(d["kind"], d["status"]) for d in pushes(session_id)] == [("nudge", "delivered")]
    laura.whatsapp("sí, tengo carnet")  # answering = reading
    stack.sim.flush()
    assert pushes(session_id)[0]["status"] == "read"


def test_accepted_then_failed_outside_the_window_falls_back_to_the_template(channel_stack, wa_provider):
    """The realistic failure: the provider accepts free-form text, then reports 63016 / 131047."""
    stack = channel_stack(provider=wa_provider, **TEMPLATE[wa_provider])
    stack.sim.twilio.templates["HXreengage"] = stack.sim.meta.templates["reengage_candidates"] = BODY
    stack.sim.faults.window_errors = "async"  # Twilio too (Meta always reports it this way)
    laura, session_id = started(stack)
    laura.close_window()

    sent = stack.api.post(f"/api/bo/candidates/{session_id}/messages",
                          json={"text": "Laura, ¿sigues interesada? Tenemos turno de mañana", "pause_bot": False}).json()
    assert sent["delivered"] is True  # accepted by the provider...
    template = laura.wait_for_whatsapp(lambda m: m.via == "template")  # ... failed later -> template
    assert template.body == "Hola Laura, soy Marisol. ¿Seguimos con tu candidatura?"
    stack.sim.flush()
    events = [e["type"] for e in stack.session(session_id)["events"]]
    assert "whatsapp_delivery_failed" in events and "whatsapp_template_fallback" in events
    failed = next(d for d in db.deliveries(session_id) if d["kind"] == "message")
    assert failed["status"] == "failed" and failed["error_code"] in (63016, 131047)

    # the candidate answers the template: the recruiter's words arrive now, nothing is lost
    replies = laura.whatsapp("sí, dime")
    assert replies[0] == "Laura, ¿sigues interesada? Tenemos turno de mañana"


def test_a_failure_reported_before_the_send_call_returns_is_not_lost(channel_stack, wa_provider):
    """Regression: the failure receipt used to arrive before the message row existed, so nothing was
    re-queued and the recruiter's message was silently lost (flaky before, deterministic here)."""
    stack = channel_stack(provider=wa_provider, **TEMPLATE[wa_provider])
    stack.sim.twilio.templates["HXreengage"] = stack.sim.meta.templates["reengage_candidates"] = BODY
    stack.sim.faults.window_errors = "async"
    stack.sim.faults.receipts_before_response = True
    laura, session_id = started(stack)
    laura.close_window()
    stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "¿Te llamo mañana?", "pause_bot": False})
    laura.wait_for_whatsapp(lambda m: m.via == "template")
    stored = [m for m in stack.session(session_id)["messages"] if m["role"] == "recruiter"][-1]
    assert stored["delivered"] == 0 and stored["channel"] is None  # queued, not marked delivered
    stack.sim.faults.receipts_before_response = False
    assert laura.whatsapp("sí, dime")[0] == "¿Te llamo mañana?"


def test_a_known_closed_window_goes_straight_to_the_template(channel_stack, wa_provider):
    stack = channel_stack(provider=wa_provider, **TEMPLATE[wa_provider])
    stack.sim.twilio.templates["HXreengage"] = stack.sim.meta.templates["reengage_candidates"] = BODY
    laura, session_id = started(stack)
    state = stack.app.store.get(session_id)
    stack.app.whatsapp_channel.set_last_inbound(state, datetime.now(timezone.utc) - timedelta(hours=30))  # yesterday
    laura.close_window()

    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    laura.wait_for_whatsapp(lambda m: m.via == "template")
    assert [d["kind"] for d in pushes(session_id)] == ["template"]  # no doomed free-form attempt first


def test_a_failed_nudge_is_replaced_not_requeued(channel_stack, wa_provider):
    """A nudge that fails outside the window is stood in for by the template: no stale 'are you there?' later."""
    stack = channel_stack(provider=wa_provider, **TEMPLATE[wa_provider])
    stack.sim.twilio.templates["HXreengage"] = stack.sim.meta.templates["reengage_candidates"] = BODY
    stack.sim.faults.window_errors = "async"
    laura, session_id = started(stack)
    laura.close_window()
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    laura.wait_for_whatsapp(lambda m: m.via == "template")
    stack.sim.flush()
    replies = laura.whatsapp("sí, sigo aquí")
    assert not any("sigues" in r.lower() for r in replies)


@pytest.mark.parametrize("fault, code", [("token", 190), ("allow_list", 131030)])
def test_graph_errors_keep_recruiter_messages_queued(channel_stack, fault, code):
    stack = meta_stack(channel_stack)
    laura, session_id = started(stack)
    if fault == "token":
        stack.sim.meta.access_token = "rotated"  # the app's token expired
    else:
        stack.sim.faults.recipient_allow_list = ["+34999999999"]  # dev-mode test number not verified
    sent = stack.api.post(f"/api/bo/candidates/{session_id}/messages", json={"text": "¿Hablamos mañana?"}).json()
    assert sent["delivered"] is False
    failure = [e for e in stack.sim.log if e.kind == "rest" and e.status and e.status >= 400][-1]
    assert failure.error.startswith(str(code))
    assert db.deliveries(session_id)[-1]["error_code"] == code


def test_unknown_template_fails_cleanly(channel_stack, caplog):
    stack = meta_stack(channel_stack, meta_template_name="not_approved")
    laura, session_id = started(stack)
    laura.close_window()
    stack.app.whatsapp_channel.set_last_inbound(stack.app.store.get(session_id),
                                                datetime.now(timezone.utc) - timedelta(hours=30))
    stack.silence(session_id, seconds=settings.reengage_first_delay + 1)
    last = stack.session(session_id)["messages"][-1]
    assert last["delivered"] == 0 and last["channel"] is None  # the nudge stays queued
    assert db.deliveries(session_id)[-1]["error_code"] == 132001
