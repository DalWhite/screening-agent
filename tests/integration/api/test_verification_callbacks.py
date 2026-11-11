"""External checks through the app: the signed provider callback and the recruiter's decisions."""

import json

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.storage import db
from backend.verifications.providers import sign

SCRIPT_BEFORE_ID = ["hola", "Carlos Martín Ruiz", "sí", "sí"]


def rows(session_id):
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM verifications WHERE session_id=? ORDER BY created_at, rowid",
                                               (session_id,))]


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
