"""Asynchronous verification worker: dispatch, provider answers, timeouts,
retries, superseding, privacy of the payload, manual decisions and the signed
webhook callback. A fake clock drives time; no network is used."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.channels.base import persist_turn
from backend.config import settings
from backend.screening.schema import Stage
from backend.storage import db
from backend.verifications.providers import ManualProvider, MockIdRegistry, WebhookProvider, sign
from backend.verifications.service import VerificationError, VerificationService

SCRIPT_BEFORE_ID = ["hola", "Carlos Martín Ruiz", "sí", "sí"]
SCRIPT_AFTER_ID = ["Getafe", "sí", "no", "3 años de albañil", "tengo coche", "el lunes"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pushed():
    return []


@pytest.fixture
def service(agent, store, clock, pushed):
    def notifier(state, text):
        if state.source == "whatsapp":
            pushed.append(text)
            return True
        return False

    providers = {"mock_id_registry": MockIdRegistry(delay_seconds=5), "manual": ManualProvider(),
                 "webhook": WebhookProvider(client=httpx.Client(transport=httpx.MockTransport(
                     lambda r: httpx.Response(202, json={"reference": "EXT-1"}))))}
    return VerificationService(agent, store, notifier=notifier, providers=providers, clock=clock)


def screen(agent, store, service, messages, profile="construction_laborer", source="web"):
    state = agent.new_session(profile_id=profile, source=source, contact="+34600000000" if source != "web" else None)
    agent.opening(state)
    store.put(state)
    db.save_session(state)
    for text in messages:
        reply, decision = agent.process(state, text)
        persist_turn(state, text, reply, decision)
        service.request_from_decision(state, decision)  # what the app's dispatch hook does
    return state


def rows(session_id):
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM verifications WHERE session_id=? ORDER BY created_at, rowid",
                                               (session_id,))]


def test_mock_registry_answers_later_and_releases_the_decision(agent, store, service, clock):
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"] + SCRIPT_AFTER_ID)
    assert state.stage == Stage.VERIFYING
    [row] = rows(state.id)
    assert row["status"] == "queued" and json.loads(row["payload_json"])["id_document"] == "12345678Z"

    service.tick()                      # dispatched: the registry answers in 5s
    assert rows(state.id)[0]["status"] == "submitted" and rows(state.id)[0]["reference"].startswith("MOCK-")
    clock.advance(3)
    service.tick()
    assert rows(state.id)[0]["status"] == "submitted"
    clock.advance(3)
    service.tick()

    [row] = rows(state.id)
    assert row["status"] == "valid" and row["payload_json"] is None  # the ID number is gone
    assert state.stage == Stage.QUALIFIED and state.data.id_check == "valid"
    stored = db.get_session(state.id)
    assert stored["status"] == "qualified"
    last = stored["messages"][-1]
    assert last["role"] == "assistant" and "cumples los requisitos" in last["content"] and last["delivered"] == 0
    assert [m["content"] for m in db.pending_messages(state.id)] == [last["content"]]  # web chat picks it up
    assert any(e["type"] == "check_result" for e in stored["events"])


def test_unknown_document_knocks_the_candidate_out_asynchronously(agent, store, service, clock):
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["99000001C", "Getafe"])
    service.tick()
    clock.advance(10)
    service.tick()
    assert state.stage == Stage.DISQUALIFIED and state.disqualify_reason == "id_not_verified"
    assert db.get_session(state.id)["pipeline_stage"] == "rejected"


def test_whatsapp_candidates_are_told_by_push(agent, store, service, clock, pushed):
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"] + SCRIPT_AFTER_ID, source="whatsapp")
    service.tick()
    clock.advance(10)
    service.tick()
    assert pushed and "cumples los requisitos" in pushed[-1]
    assert db.get_session(state.id)["messages"][-1]["delivered"] == 1


def test_timeout_becomes_an_error_that_can_be_retried(agent, store, service, clock):
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"] + SCRIPT_AFTER_ID)
    service.providers["mock_id_registry"] = ManualProvider()  # nobody ever answers
    service.tick()
    clock.advance(301)  # the profile gives the registry 300s
    service.tick()
    [row] = rows(state.id)
    assert row["status"] == "error" and row["decided_by"] == "timeout"
    assert row["payload_json"] is not None  # kept only so that a retry is possible
    assert state.stage == Stage.QUALIFIED and "check_error:id_check" in state.flags
    assert db.get_session(state.id)["pipeline_stage"] == "review"

    new_id = service.retry(row["id"])
    old, new = rows(state.id)
    assert old["payload_json"] is None and new["id"] == new_id and new["status"] == "queued"
    assert state.data.id_check == "pending" and "check_error:id_check" not in state.flags
    with pytest.raises(VerificationError):
        service.retry(row["id"])


def test_transient_provider_failures_are_retried_with_backoff(agent, store, service, clock, monkeypatch):
    calls = []

    def flaky(request):
        calls.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(settings, "verification_webhook_url", "https://checks.example/api")
    service.providers["mock_id_registry"] = WebhookProvider(client=httpx.Client(transport=httpx.MockTransport(flaky)))
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"])
    service.tick()
    [row] = rows(state.id)
    assert row["status"] == "queued" and row["attempts"] == 1 and len(calls) == 1
    service.tick()                      # still inside the backoff window: no new call
    assert len(calls) == 1
    clock.advance(11)
    service.tick()
    clock.advance(21)
    service.tick()                      # third failure = max attempts
    [row] = rows(state.id)
    assert row["status"] == "error" and row["attempts"] == 3 and len(calls) == 3


def test_webhook_request_is_signed_and_carries_a_callback(agent, store, service, clock, monkeypatch):
    seen = []

    def provider(request):
        seen.append(request)
        return httpx.Response(202, json={"reference": "EXT-42"})

    monkeypatch.setattr(settings, "verification_webhook_url", "https://checks.example/api")
    monkeypatch.setattr(settings, "verification_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "public_base_url", "https://screening.example")
    service.providers["mock_id_registry"] = WebhookProvider(client=httpx.Client(transport=httpx.MockTransport(provider)))
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"])
    service.tick()
    body = json.loads(seen[0].content)
    assert body["payload"]["id_document"] == "12345678Z"
    assert body["callback_url"] == f"https://screening.example/api/verifications/{body['verification_id']}/callback"
    assert seen[0].headers["X-Signature"] == sign(seen[0].content, "s3cret")
    assert rows(state.id)[0]["reference"] == "EXT-42" and rows(state.id)[0]["status"] == "submitted"


def test_a_new_answer_supersedes_the_running_check(agent, store, service):
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"])
    first = rows(state.id)[0]["id"]
    second = service.request(state, "id_check", {"id_document": "X1234567L", "full_name": "x"})
    old, new = rows(state.id)
    assert old["status"] == "superseded" and old["payload_json"] is None and new["id"] == second
    with pytest.raises(VerificationError):
        service.complete(first, "valid")


def test_manual_checks_wait_for_a_recruiter(agent, store, service, clock):
    state = screen(agent, store, service, ["hola", "Laura Jiménez Ortiz", "sí, tengo carnet"], profile="delivery_courier")
    for _ in range(3):
        service.tick()
        clock.advance(3600)
    [row] = rows(state.id)
    assert row["provider"] == "manual" and row["status"] == "submitted"
    service.complete(row["id"], "invalid", "Carnet caducado", by="Ana")
    [row] = rows(state.id)
    assert row["status"] == "invalid" and row["decided_by"] == "Ana" and row["payload_json"] is None
    assert "license_not_verified" in state.flags


# ---------- webhook callback endpoint ----------

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "cb.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "verification_tick_seconds", 3600)  # the test drives the worker
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app) as client:
        yield client, appmod


def _webhook_verification(client, appmod):
    sid = client.post("/api/sessions", json={"profile_id": "construction_laborer"}).json()["session_id"]
    for text in SCRIPT_BEFORE_ID + ["12345678Z"]:
        client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    [row] = rows(sid)
    with db.get_conn() as conn:  # pretend this check is served by an external webhook provider
        conn.execute("UPDATE verifications SET provider='webhook', status='submitted' WHERE id=?", (row["id"],))
    return sid, row["id"]


def test_callbacks_are_rejected_without_a_configured_secret(app_client, monkeypatch):
    client, appmod = app_client
    monkeypatch.setattr(settings, "verification_webhook_secret", "")
    sid, vid = _webhook_verification(client, appmod)
    resp = client.post(f"/api/verifications/{vid}/callback", json={"status": "valid"})
    assert resp.status_code == 403


def test_signed_callback_applies_the_result(app_client, monkeypatch):
    client, appmod = app_client
    monkeypatch.setattr(settings, "verification_webhook_secret", "s3cret")
    sid, vid = _webhook_verification(client, appmod)
    body = json.dumps({"status": "invalid", "detail": "Documento caducado", "reference": "EXT-9"}).encode()
    assert client.post(f"/api/verifications/{vid}/callback", content=body,
                       headers={"X-Signature": "sha256=forged"}).status_code == 403
    resp = client.post(f"/api/verifications/{vid}/callback", content=body,
                       headers={"X-Signature": sign(body, "s3cret"), "Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json()["status"] == "invalid"
    session = db.get_session(sid)
    assert session["status"] == "disqualified" and session["disqualify_reason"] == "id_not_verified"
    assert rows(sid)[0]["reference"] == "EXT-9"
    assert client.post("/api/verifications/nope/callback", content=body,
                       headers={"X-Signature": sign(body, "s3cret")}).status_code == 404


def test_recruiter_decides_and_retries_from_the_backoffice(app_client):
    client, appmod = app_client
    sid = client.post("/api/sessions", json={"profile_id": "delivery_courier"}).json()["session_id"]
    for text in ["hola", "Laura Jiménez Ortiz", "sí, tengo carnet"]:
        client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    appmod.app.state.verifications.tick()
    detail = client.get(f"/api/bo/candidates/{sid}").json()
    [check] = detail["report"]["checks"]
    assert check["id"] == "license_check" and check["status"] == "pending" and check["provider"] == "manual"
    vid = check["verification"]["id"]
    resp = client.post(f"/api/bo/candidates/{sid}/verifications/{vid}/decision",
                       json={"status": "valid", "note": "Carnet visto en la entrevista"}, headers={"X-Recruiter": "Ana"})
    assert resp.status_code == 200 and resp.json()["status"] == "valid"
    detail = client.get(f"/api/bo/candidates/{sid}").json()
    assert detail["report"]["checks"][0]["status"] == "valid"
    assert detail["report"]["checks"][0]["verification"]["decided_by"] == "Ana"
    assert client.post(f"/api/bo/candidates/{sid}/verifications/{vid}/decision",
                       json={"status": "maybe"}).status_code == 422
    assert client.post(f"/api/bo/candidates/{sid}/verifications/{vid}/retry").status_code == 422
    assert client.post(f"/api/bo/candidates/other/verifications/{vid}/retry").status_code == 404


def test_recruiter_correcting_an_id_triggers_a_new_verification(app_client):
    client, appmod = app_client
    sid = client.post("/api/sessions", json={"profile_id": "construction_laborer"}).json()["session_id"]
    for text in SCRIPT_BEFORE_ID + ["12345678Z"]:
        client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    resp = client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"id_document": "X1234567L"}})
    assert resp.status_code == 200
    assert resp.json()["data"]["id_document"] == "•••••567L" and resp.json()["data"]["id_document_type"] == "nie"
    old, new = rows(sid)
    assert old["status"] == "superseded" and json.loads(new["payload_json"])["id_document"] == "X1234567L"
    bad = client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"id_document": "X1234567A"}})
    assert bad.status_code == 422
    assert client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"id_check": "valid"}}).status_code == 422


def test_webhook_destinations_are_restricted_to_allowed_hosts(agent, store, service, monkeypatch):
    from backend.verifications.providers import webhook_url_allowed

    monkeypatch.setattr(settings, "verification_webhook_url", "https://checks.example/api")
    monkeypatch.setattr(settings, "verification_allowed_hosts", "registry.partner.es")
    assert webhook_url_allowed("https://checks.example/other")
    assert webhook_url_allowed("https://registry.partner.es/v1")
    assert not webhook_url_allowed("https://evil.example/collect")
    assert not webhook_url_allowed("http://registry.partner.es/v1")  # no plain HTTP
    assert webhook_url_allowed("http://localhost:9000/x") is False   # localhost only when allowed explicitly
    monkeypatch.setattr(settings, "verification_allowed_hosts", "localhost")
    assert webhook_url_allowed("http://localhost:9000/x")

    sent = []
    monkeypatch.setattr(settings, "verification_allowed_hosts", "")
    service.providers["mock_id_registry"] = WebhookProvider(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: sent.append(r) or httpx.Response(202))))
    state = screen(agent, store, service, SCRIPT_BEFORE_ID + ["12345678Z"])
    profile = agent.profile(state)
    check = profile.check("id_check")
    object.__setattr__(check, "url", "https://evil.example/collect")
    try:
        service.tick()
    finally:
        object.__setattr__(check, "url", None)
    [row] = rows(state.id)
    assert sent == [] and row["status"] == "error" and "no permitido" in row["result_detail"]
