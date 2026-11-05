"""Configurable pipeline stages and analytics ↔ candidate-list drill-down."""

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.storage import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "stages.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "backoffice_password", "")
    monkeypatch.setattr(settings, "verification_mock_delay_seconds", 0)
    monkeypatch.setattr(settings, "verification_tick_seconds", 0.05)
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app, headers={"X-Recruiter": "Ana"}) as test_client:
        yield test_client


def chat(client, messages, profile_id=None):
    sid = client.post("/api/sessions", json={"profile_id": profile_id}).json()["session_id"]
    for text in messages:
        client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    return sid


def stage_ids(client):
    return [s["id"] for s in client.get("/api/bo/stages").json()["stages"]]


def test_default_stages_and_system_flags(client):
    stages = client.get("/api/bo/stages").json()["stages"]
    assert [s["id"] for s in stages] == ["screening", "review", "qualified", "shortlisted", "interview", "offer",
                                         "hired", "rejected"]
    assert {s["id"] for s in stages if s["system"]} == {"screening", "review", "qualified", "rejected"}
    assert client.get("/api/bo/meta").json()["pipeline"][0]["label"] == "En screening"


def test_create_rename_recolor_and_reorder(client):
    created = client.post("/api/bo/stages", json={"label": "Prueba técnica", "color": "pink"}).json()
    assert created["id"] == "prueba_tecnica"
    assert stage_ids(client)[-2:] == ["prueba_tecnica", "rejected"]  # before the final "rejected" column
    after = client.post("/api/bo/stages", json={"label": "Llamada", "after": "review"}).json()
    assert stage_ids(client).index(after["id"]) == stage_ids(client).index("review") + 1
    renamed = client.patch("/api/bo/stages/qualified", json={"label": "Apto (bot)", "color": "green"}).json()
    assert renamed == {"id": "qualified", "label": "Apto (bot)", "color": "green"}
    order = list(reversed(stage_ids(client)))
    assert [s["id"] for s in client.put("/api/bo/stages/order", json={"ids": order}).json()["stages"]] == order
    assert client.post("/api/bo/stages", json={"label": "apto (BOT)"}).status_code == 422  # duplicate name
    assert client.patch("/api/bo/stages/qualified", json={"color": "neon"}).status_code == 422
    assert client.put("/api/bo/stages/order", json={"ids": ["screening"]}).status_code == 422
    assert client.patch("/api/bo/stages/nope", json={"label": "x"}).status_code == 404


def test_custom_stages_are_usable_and_deletable(client):
    sid = chat(client, ["hola", "Pedro Sánchez", "no tengo carnet"])
    custom = client.post("/api/bo/stages", json={"label": "Llamada"}).json()["id"]
    assert client.patch(f"/api/bo/candidates/{sid}", json={"pipeline_stage": custom}).status_code == 200
    board = {c["id"]: c["count"] for c in client.get("/api/bo/pipeline").json()["columns"]}
    assert board[custom] == 1
    assert client.delete(f"/api/bo/stages/{custom}").status_code == 422  # candidates need a destination
    resp = client.delete(f"/api/bo/stages/{custom}", params={"move_to": "shortlisted"})
    assert resp.json() == {"deleted": custom, "moved": 1}
    assert db.get_session(sid)["pipeline_stage"] == "shortlisted"
    events = client.get(f"/api/bo/candidates/{sid}").json()["events"]
    assert any(e["type"] == "pipeline_change" and e["payload"].get("reason") == "stage_deleted" for e in events)
    assert client.delete("/api/bo/stages/rejected").status_code == 422  # system stage
    assert client.patch(f"/api/bo/candidates/{sid}", json={"pipeline_stage": custom}).status_code == 422


def test_analytics_accepts_candidate_filters_and_drills_down(client):
    chat(client, ["hola", "Laura Jiménez Ortiz", "sí, tengo carnet", "Madrid", "jornada completa", "flexible", "2", "mañana"])
    chat(client, ["hola", "Pedro Sánchez", "no tengo carnet"])
    chat(client, ["hola", "Ana Ruiz", "sí", "Ourense", "Vigo"])
    chat(client, ["hola", "Lucía Pérez", "tengo 17 años"], "construction_laborer")
    sid = chat(client, ["hola", "Marta Núñez Vidal", "sí", "sí"], "construction_laborer")
    client.post(f"/api/sessions/{sid}/end")  # abandoned at the ID question
    chat(client, ["hola", "Rosa Gil", "¿Cuánto se cobra?"], "construction_laborer")

    courier = client.get("/api/bo/analytics", params={"profile_id": "delivery_courier"}).json()
    assert courier["total_sessions"] == 3 and courier["disqualify_reasons"] == {"no_license": 1, "out_of_area": 1}

    def count(**params):
        return client.get("/api/bo/candidates", params=params).json()["total"]

    # every number in analytics can be opened as a candidate list with the same filters
    assert count(profile_id="delivery_courier", status="disqualified", disqualify_reason="no_license") == 1
    construction = client.get("/api/bo/analytics", params={"profile_id": "construction_laborer"}).json()
    assert construction["dropoff_by_stage"] == {"id_document": 1}
    assert count(profile_id="construction_laborer", status="abandoned", dropped_at="id_document") == 1
    assert construction["funnel"]["work_permit"] == count(profile_id="construction_laborer", reached="work_permit") == 1
    assert courier["funnel"]["qualified"] == count(profile_id="delivery_courier", reached="qualified") == 1
    assert construction["top_rag_hits"]
    for faq_id, n in construction["top_rag_hits"].items():
        assert count(faq=faq_id) == n == 1
    assert construction["languages"] == {"es": 3} and count(profile_id="construction_laborer", language="es") == 3
    for stage, n in courier["pipeline"].items():
        assert count(profile_id="delivery_courier", pipeline_stage=stage) == n

    # filters narrow the analytics themselves
    today = db.get_session(sid)["started_at"][:10]
    assert client.get("/api/bo/analytics", params={"started_from": "2000-01-01", "started_to": "2000-01-02"}).json()["total_sessions"] == 0
    assert client.get("/api/bo/analytics", params={"started_from": today}).json()["total_sessions"] == 6
    assert client.get("/api/bo/analytics", params={"status": "disqualified"}).json()["total_sessions"] == 3
