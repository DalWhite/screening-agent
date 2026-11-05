"""Recruiting-process CRUD: drafts, problems per cell, publication as revisions,
sessions pinned to their revision, history/restore, create/duplicate/delete,
(de)activation, the rule simulator and seed re-imports."""

import copy

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.screening.profile import load_seed
from backend.storage import db
from backend.storage.profile_store import ProfileStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "profiles.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "backoffice_password", "")
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app, headers={"X-Recruiter": "Ana"}) as test_client:
        yield test_client


def working(client, pid):
    resp = client.get(f"/api/bo/profiles/{pid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def save(client, pid, data, base=None):
    return client.put(f"/api/bo/profiles/{pid}/draft", json={"profile": data, "base_revision": base})


def chat(client, profile_id, messages=()):
    resp = client.post("/api/sessions", json={"profile_id": profile_id})
    sid = resp.json()["session_id"]
    replies = [resp.json()["reply"]]
    for text in messages:
        replies.append(client.post(f"/api/sessions/{sid}/messages", json={"text": text}).json()["reply"])
    return sid, replies


def test_seeds_are_imported_as_revision_one(client):
    listed = {p["id"]: p for p in client.get("/api/bo/profiles").json()["profiles"]}
    assert listed["construction_laborer"]["latest_revision"] == 1 and listed["construction_laborer"]["published"]
    assert listed["construction_laborer"]["questions"] == 10 and listed["construction_laborer"]["checks"] == 1
    detail = working(client, "construction_laborer")
    assert detail["problems"] == [] and detail["draft"] is None
    assert detail["versions"][0]["revision"] == 1 and detail["versions"][0]["published_by"] == "seed"
    assert "id_document" in detail["schema"]["question_types"]


def test_draft_then_publish_creates_a_revision_and_pins_running_sessions(client):
    old_sid, _ = chat(client, "construction_laborer", ["hola", "Carlos Martín Ruiz"])
    data = working(client, "construction_laborer")["working"]
    data["intro"]["es"] = ["¡Hola! Soy {name}. Versión nueva del proceso. ¿Empezamos?"]
    data["questions"][1]["prompts"]["es"] = ["¿Tienes 18 años cumplidos?"]
    resp = save(client, "construction_laborer", data)
    assert resp.status_code == 200 and resp.json() == {"draft_revision": 1, "problems": []}

    # drafts are not live
    assert "Versión nueva" not in chat(client, "construction_laborer")[1][0]
    resp = client.post("/api/bo/profiles/construction_laborer/publish", json={"note": "Saludo nuevo"})
    assert resp.status_code == 200 and resp.json()["revision"] == 2

    new_sid, replies = chat(client, "construction_laborer", ["hola", "Ana Gil Mora"])
    assert "Versión nueva" in replies[0] and replies[-1] == "¿Tienes 18 años cumplidos?"
    # the candidate who started before keeps the rules they started with
    reply = client.post(f"/api/sessions/{old_sid}/messages", json={"text": "sí"}).json()["reply"]
    assert "permiso para trabajar" in reply
    assert db.get_session(old_sid)["profile_revision"] == 1 and db.get_session(new_sid)["profile_revision"] == 2
    assert client.get(f"/api/bo/candidates/{old_sid}").json()["profile"]["revision"] == 1
    detail = working(client, "construction_laborer")
    assert detail["draft"] is None and [v["revision"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["note"] == "Saludo nuevo" and detail["versions"][0]["published_by"] == "Ana"


def test_problems_point_at_the_cell_and_block_publication(client):
    data = working(client, "construction_laborer")["working"]
    data["rules"][0]["field"] = "edad"
    data["scoring"][0]["points"] = "mucho"
    body = save(client, "construction_laborer", data).json()
    paths = {tuple(p["path"]) for p in body["problems"]}
    assert ("scoring", 0, "points") in paths
    data["scoring"][0]["points"] = 20
    body = save(client, "construction_laborer", data, base=body["draft_revision"]).json()
    assert body["problems"] == [{"path": ["rules", 0, "field"], "message": "la regla «underage» usa un dato desconocido «edad»"}]
    resp = client.post("/api/bo/profiles/construction_laborer/publish", json={})
    assert resp.status_code == 422 and resp.json()["detail"]["problems"][0]["path"] == ["rules", 0, "field"]


def test_concurrent_edits_are_detected(client):
    data = working(client, "delivery_courier")["working"]
    first = save(client, "delivery_courier", data, base=0).json()["draft_revision"]
    assert save(client, "delivery_courier", data, base=first).status_code == 200
    stale = save(client, "delivery_courier", data, base=first)
    assert stale.status_code == 409


def test_the_process_id_cannot_be_renamed(client):
    data = working(client, "delivery_courier")["working"]
    data["id"] = "hacked"
    save(client, "delivery_courier", data)
    assert working(client, "delivery_courier")["draft"]["id"] == "delivery_courier"


def test_discard_and_restore(client):
    original = working(client, "delivery_courier")["working"]
    changed = copy.deepcopy(original)
    changed["company"] = "Otra Empresa"
    save(client, "delivery_courier", changed)
    assert client.delete("/api/bo/profiles/delivery_courier/draft").status_code == 204
    assert working(client, "delivery_courier")["draft"] is None
    save(client, "delivery_courier", changed)
    client.post("/api/bo/profiles/delivery_courier/publish", json={})
    assert client.post("/api/bo/profiles/delivery_courier/versions/1/restore").status_code == 200
    detail = working(client, "delivery_courier")
    assert detail["draft"]["company"] == "Grupo Sazón" and detail["latest_revision"] == 2
    exported = client.get("/api/bo/profiles/delivery_courier/versions/2")
    assert exported.headers["content-disposition"].endswith('delivery_courier-r2.json"')
    assert exported.json()["company"] == "Otra Empresa"


def test_create_blank_process_publish_and_screen(client):
    resp = client.post("/api/bo/profiles", json={"id": "warehouse_picker", "company": "Almacenes Norte",
                                                 "role": "Mozo de almacén"})
    assert resp.status_code == 201 and resp.json()["problems"] == []
    assert "warehouse_picker" not in {p["id"] for p in client.get("/api/profiles").json()["profiles"]}
    assert client.post("/api/sessions", json={"profile_id": "warehouse_picker"}).status_code == 404
    assert client.delete("/api/bo/profiles/warehouse_picker/draft").status_code == 422  # never published

    assert client.post("/api/bo/profiles/warehouse_picker/publish", json={}).json()["revision"] == 1
    assert "warehouse_picker" in {p["id"] for p in client.get("/api/profiles").json()["profiles"]}
    sid, replies = chat(client, "warehouse_picker", ["hola", "Pedro Gómez Ruiz", "mañana"])
    assert "Almacenes Norte" in replies[0] and "Mozo de almacén" in replies[0]
    assert db.get_session(sid)["status"] == "qualified"


def test_duplicate_delete_and_validation(client):
    resp = client.post("/api/bo/profiles", json={"id": "construction_madrid", "from_profile": "construction_laborer",
                                                 "company": "Horizonte Madrid"})
    assert resp.status_code == 201
    draft = resp.json()["draft"]
    assert draft["company"] == "Horizonte Madrid" and len(draft["questions"]) == 10 and draft["id"] == "construction_madrid"
    assert client.post("/api/bo/profiles", json={"id": "construction_madrid", "company": "x", "role": "y"}).status_code == 422
    assert client.post("/api/bo/profiles", json={"id": "Bad Id!", "company": "x", "role": "y"}).status_code == 422
    assert client.post("/api/bo/profiles", json={"id": "nothing", "from_profile": "nope"}).status_code == 404
    assert client.delete("/api/bo/profiles/construction_madrid").status_code == 204
    chat(client, "construction_laborer")
    assert client.delete("/api/bo/profiles/construction_laborer").status_code == 409  # has candidates


def test_deactivate_hides_the_process_from_candidates(client):
    resp = client.post("/api/bo/profiles/construction_laborer/active", json={"active": False})
    assert resp.status_code == 200 and resp.json()["revision"] == 2
    assert "construction_laborer" not in {p["id"] for p in client.get("/api/profiles").json()["profiles"]}
    assert client.post("/api/sessions", json={"profile_id": "construction_laborer"}).status_code == 404
    listed = {p["id"]: p for p in client.get("/api/bo/profiles").json()["profiles"]}
    assert listed["construction_laborer"]["active"] is False
    client.post("/api/bo/profiles/construction_laborer/active", json={"active": True})
    assert client.post("/api/sessions", json={"profile_id": "construction_laborer"}).status_code == 200


def test_simulator_runs_the_draft_rules(client):
    data = working(client, "construction_laborer")["working"]
    data["rules"].append({"id": "no_transport_ko", "action": "disqualify", "label": {"es": "Sin transporte"},
                          "field": "transport", "op": "eq", "value": "none",
                          "message": {"es": "Necesitamos que puedas llegar a la obra."}})
    save(client, "construction_laborer", data)
    script = ["hola", "Ana Gil Mora", "sí", "sí", "12345678Z", "Toledo", "sí", "sí", "0", "voy andando"]
    result = client.post("/api/bo/profiles/construction_laborer/simulate", json={"messages": script}).json()
    assert result["source"] == "draft" and result["status"] == "disqualified"
    assert result["disqualify_reason"] == "no_transport_ko"
    assert any("Verificación «id_check»: valid" in m["content"] for m in result["transcript"] if m["role"] == "system")
    published = client.post("/api/bo/profiles/construction_laborer/simulate",
                            json={"messages": script, "source": "published"}).json()
    assert published["status"] == "active" and published["stage"] == "start_date"
    unknown = client.post("/api/bo/profiles/construction_laborer/simulate",
                          json={"messages": ["hola", "Ana Gil Mora", "sí", "sí", "99000001C"]}).json()
    assert unknown["status"] == "disqualified" and unknown["disqualify_reason"] == "id_not_verified"
    assert not db.list_sessions(profile_id="construction_laborer")  # nothing stored


def test_changed_seed_files_become_a_new_revision(temp_db):
    from backend.config import settings as cfg
    from pathlib import Path

    store = ProfileStore()
    seed = load_seed(Path(cfg.profiles_dir) / "delivery_courier" / "profile.json")
    store.sync_seeds({"delivery_courier": seed})
    store.sync_seeds({"delivery_courier": seed})  # unchanged: nothing new
    assert store.get("delivery_courier")["latest_revision"] == 1
    changed = copy.deepcopy(seed)
    changed["company"] = "Grupo Sazón SL"
    store.sync_seeds({"delivery_courier": changed})
    detail = store.get("delivery_courier")
    assert detail["latest_revision"] == 2 and detail["published"]["company"] == "Grupo Sazón SL"
    assert detail["versions"][0]["note"] == "Semilla actualizada en el repositorio"


def test_revisions_published_by_another_process_are_picked_up(client, monkeypatch):
    from backend.screening.profile import registry

    reg = registry()
    other = ProfileStore()  # what another app process would do: publish straight into the database
    data = working(client, "delivery_courier")["working"]
    data["company"] = "Sazón desde otro proceso"
    other.save_draft("delivery_courier", data, "otro", None)
    other.publish("delivery_courier", data, "otro")
    assert reg.get("delivery_courier").company == "Grupo Sazón"  # not reloaded yet
    reg._checked_at = 0  # throttle window elapsed
    names = {p["id"]: p["company"] for p in client.get("/api/profiles").json()["profiles"]}
    assert names["delivery_courier"] == "Sazón desde otro proceso"
