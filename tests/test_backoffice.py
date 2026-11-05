"""Recruiter backoffice API: search, pipeline, candidate views and manual interaction.

The whole FastAPI app is booted with the MockLLM and a throwaway database;
candidates are created through the public chat API exactly as the web chat does.
"""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.storage import db

COURIER_OK = ["hola", "Laura Jiménez Ortiz", "sí, tengo carnet", "Madrid, Malasaña", "jornada completa",
              "flexible", "2 años en Glovo", "mañana"]
COURIER_NO_LICENSE = ["hola", "Pedro Sánchez", "no tengo carnet"]
BUILDER_OK = ["hola", "Carlos Martín Ruiz", "sí", "sí", "12345678Z", "Getafe", "sí", "no", "3 años de albañil",
              "tengo coche", "en 2 semanas"]
BUILDER_PHYSICAL = ["hola", "Rosa Gil Mora", "sí", "sí", "X1234567L", "Sevilla", "no puedo cargar peso", "sí", "0",
                    "en bus", "mañana"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bo.db"))
    monkeypatch.setattr(settings, "llm_fallback", "mock")
    monkeypatch.setattr(settings, "backoffice_password", "")
    # external checks: the mock ID registry answers at once, the worker ticks fast
    monkeypatch.setattr(settings, "verification_mock_delay_seconds", 0)
    monkeypatch.setattr(settings, "verification_tick_seconds", 0.05)
    import backend.app as appmod

    appmod.store.registry.clear()
    with TestClient(appmod.app) as test_client:
        yield test_client


def chat(client, messages, profile_id=None):
    sid = client.post("/api/sessions", json={"source": "web", "profile_id": profile_id}).json()["session_id"]
    last = None
    for text in messages:
        last = client.post(f"/api/sessions/{sid}/messages", json={"text": text}).json()
    return sid, last


def wait_for_checks(client, timeout=5.0):
    """Block until no candidate is waiting for an external check."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/bo/candidates", params={"status": "verifying"}).json()["total"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("external checks did not finish")


@pytest.fixture
def seeded(client):
    ids = {
        "laura": chat(client, COURIER_OK)[0],
        "pedro": chat(client, COURIER_NO_LICENSE)[0],
        "carlos": chat(client, BUILDER_OK, "construction_laborer")[0],
        "rosa": chat(client, BUILDER_PHYSICAL, "construction_laborer")[0],
        "ongoing": chat(client, ["hola", "Marta Núñez Vidal"], "construction_laborer")[0],
    }
    wait_for_checks(client)
    return ids


def search(client, **params):
    resp = client.get("/api/bo/candidates", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def names(result):
    return sorted(item["name"] or "" for item in result["items"])


# ---------- public profile API + candidate chat ----------

def test_profiles_endpoint_lists_active_processes(client):
    body = client.get("/api/profiles").json()
    ids = {p["id"] for p in body["profiles"]}
    assert {"delivery_courier", "construction_laborer"} <= ids and body["default"] == "delivery_courier"


def test_chat_session_runs_the_requested_profile(client):
    resp = client.post("/api/sessions", json={"profile_id": "construction_laborer"}).json()
    assert "Constructora Horizonte" in resp["reply"] and resp["profile_id"] == "construction_laborer"
    assert client.post("/api/sessions", json={"profile_id": "nope"}).status_code == 404


def test_candidate_endpoint_hides_recruiter_data(client, seeded):
    sid = seeded["carlos"]
    client.patch(f"/api/bo/candidates/{sid}", json={"tags": ["urgente"], "assignee": "Ana"})
    client.post(f"/api/bo/candidates/{sid}/notes", json={"body": "Buen perfil"})
    public = client.get(f"/api/sessions/{sid}").json()
    assert "tags" not in public and "assignee" not in public and "score" not in public
    assert "Buen perfil" not in str(public)
    assert public["profile"]["company"] == "Constructora Horizonte"


# ---------- search & filtering ----------

def test_search_by_name_is_accent_insensitive(client, seeded):
    assert names(search(client, q="jimenez")) == ["Laura Jiménez Ortiz"]
    assert names(search(client, q="NUÑEZ")) == ["Marta Núñez Vidal"]


def test_search_matches_transcript_and_city(client, seeded):
    assert names(search(client, q="albañil")) == ["Carlos Martín Ruiz"]
    assert names(search(client, city="sevilla")) == ["Rosa Gil Mora"]


def test_filter_by_profile_status_and_pipeline(client, seeded):
    result = search(client, profile_id="construction_laborer")
    assert result["total"] == 3
    assert result["facets"]["profile_id"] == {"construction_laborer": 3, "delivery_courier": 2}
    assert names(search(client, status="disqualified")) == ["Pedro Sánchez"]
    assert names(search(client, pipeline_stage="review")) == ["Rosa Gil Mora"]  # physical_review flag
    assert names(search(client, pipeline_stage="qualified")) == ["Carlos Martín Ruiz", "Laura Jiménez Ortiz"]
    assert names(search(client, pipeline_stage="qualified,rejected")) == [
        "Carlos Martín Ruiz", "Laura Jiménez Ortiz", "Pedro Sánchez"]


def test_filter_by_flag_score_and_sort(client, seeded):
    assert names(search(client, flag="needs_prl_training")) == ["Carlos Martín Ruiz"]
    assert names(search(client, flag="physical_review")) == ["Rosa Gil Mora"]
    scored = search(client, min_score=50, sort="score", order="desc")
    assert [i["name"] for i in scored["items"]] == ["Laura Jiménez Ortiz", "Carlos Martín Ruiz"]
    assert all(i["score"] >= 50 for i in scored["items"])
    low = search(client, max_score=10)
    assert "Pedro Sánchez" in names(low)


def test_filter_by_tag_assignee_and_date(client, seeded):
    client.patch(f"/api/bo/candidates/{seeded['laura']}", json={"tags": ["moto propia"], "assignee": "Ana"})
    assert names(search(client, tag="moto propia")) == ["Laura Jiménez Ortiz"]
    assert names(search(client, assignee="Ana")) == ["Laura Jiménez Ortiz"]
    assert search(client, assignee="__none__")["total"] == 4
    today = db.get_session(seeded["laura"])["started_at"][:10]
    assert search(client, started_from=today, started_to=today)["total"] == 5
    assert search(client, started_from="2000-01-01", started_to="2000-01-02")["total"] == 0


def test_pagination(client, seeded):
    page1 = search(client, page_size=2, page=1, sort="name", order="asc")
    page3 = search(client, page_size=2, page=3, sort="name", order="asc")
    assert page1["total"] == 5 and len(page1["items"]) == 2 and len(page3["items"]) == 1
    assert page1["items"][0]["name"] == "Carlos Martín Ruiz"


def test_invalid_filters_are_rejected(client):
    assert client.get("/api/bo/candidates", params={"sort": "DROP TABLE"}).status_code == 422
    assert client.get("/api/bo/candidates", params={"min_score": 500}).status_code == 422


def test_csv_export_respects_filters(client, seeded):
    resp = client.get("/api/bo/candidates.csv", params={"profile_id": "construction_laborer"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert {r["name"] for r in rows} == {"Carlos Martín Ruiz", "Rosa Gil Mora", "Marta Núñez Vidal"}
    carlos = next(r for r in rows if r["name"] == "Carlos Martín Ruiz")
    assert carlos["transport"] == "own_vehicle" and carlos["has_prl_course"] == "no"
    assert carlos["id_check"] == "valid" and carlos["id_document"] == "•••••678Z"
    assert "12345678Z" not in resp.text  # the full ID number never leaves the verification payload


def test_csv_export_neutralises_formulas(client, seeded):
    sid = seeded["laura"]
    client.patch(f"/api/bo/candidates/{sid}", json={"assignee": "=HYPERLINK(\"http://evil\")"})
    rows = list(csv.DictReader(io.StringIO(client.get("/api/bo/candidates.csv").text)))
    laura = next(r for r in rows if r["id"] == sid)
    assert laura["assignee"].startswith("'=")


# ---------- pipeline ----------

def test_pipeline_board_groups_by_stage(client, seeded):
    board = client.get("/api/bo/pipeline").json()["columns"]
    counts = {c["id"]: c["count"] for c in board}
    assert counts == {"screening": 1, "review": 1, "qualified": 2, "shortlisted": 0, "interview": 0,
                      "offer": 0, "hired": 0, "rejected": 1}
    board = client.get("/api/bo/pipeline", params={"profile_id": "delivery_courier"}).json()["columns"]
    assert sum(c["count"] for c in board) == 2


def test_manual_move_sticks_and_is_audited(client, seeded):
    sid = seeded["laura"]
    resp = client.patch(f"/api/bo/candidates/{sid}", json={"pipeline_stage": "interview",
                                                            "interview_at": "2026-11-20T10:00"},
                        headers={"X-Recruiter": "Ana%20Ruiz"})
    assert resp.status_code == 200 and resp.json()["pipeline_stage"] == "interview"
    detail = client.get(f"/api/bo/candidates/{sid}").json()
    assert detail["pipeline_stage"] == "interview" and detail["interview_at"] == "2026-11-20T10:00"
    change = next(e for e in detail["events"] if e["type"] == "pipeline_change")
    assert change["payload"] == {"from": "qualified", "to": "interview", "by": "Ana Ruiz"}
    # later agent saves never override a recruiter decision
    import backend.app as appmod
    db.save_session(appmod.store.get(sid))
    assert db.get_session(sid)["pipeline_stage"] == "interview"


def test_invalid_pipeline_stage(client, seeded):
    resp = client.patch(f"/api/bo/candidates/{seeded['laura']}", json={"pipeline_stage": "promoted"})
    assert resp.status_code == 422
    assert client.patch("/api/bo/candidates/missing", json={"pipeline_stage": "offer"}).status_code == 404


def test_bulk_move_and_tag(client, seeded):
    ids = [seeded["laura"], seeded["carlos"]]
    assert client.post("/api/bo/candidates/bulk", json={"ids": ids, "action": "move", "value": "shortlisted"}).json()["count"] == 2
    client.post("/api/bo/candidates/bulk", json={"ids": ids, "action": "tag", "value": "campaña-nov"})
    assert names(search(client, pipeline_stage="shortlisted", tag="campaña-nov")) == [
        "Carlos Martín Ruiz", "Laura Jiménez Ortiz"]


# ---------- candidate profile + screening results ----------

def test_candidate_detail_contains_screening_report(client, seeded):
    detail = client.get(f"/api/bo/candidates/{seeded['carlos']}").json()
    report = detail["report"]
    assert report["outcome"]["status"] == "qualified"
    answers = {a["id"]: a for a in report["answers"]}
    assert answers["location"]["display"] == "Madrid (Getafe), ES"
    assert answers["transport"]["display"] == "Vehículo propio"
    assert answers["experience"]["raw_answers"] == ["3 años de albañil"]
    rules = {r["id"]: r["result"] for r in report["rules"]}
    assert rules["underage"] == "passed" and rules["needs_prl_training"] == "triggered"
    assert report["score"] == detail["score"]
    assert any(b["label"] == "Vehículo propio" and b["points"] == 20 for b in report["score_breakdown"])
    assert {"id": "needs_prl_training", "label": "Necesita curso PRL"} in report["flags"]
    assert detail["profile"]["company"] == "Constructora Horizonte"


def test_knockout_is_visible_in_report(client, seeded):
    report = client.get(f"/api/bo/candidates/{seeded['pedro']}").json()["report"]
    assert report["outcome"]["disqualify_label"] == "Sin licencia de conducir"
    answers = {a["id"]: a["status"] for a in report["answers"]}
    assert answers["license"] == "knockout" and answers["location"] == "missing"


def test_in_progress_report_marks_pending_question(client, seeded):
    detail = client.get(f"/api/bo/candidates/{seeded['ongoing']}").json()
    answers = {a["id"]: a["status"] for a in detail["report"]["answers"]}
    assert answers["name"] == "answered" and answers["adult"] == "pending"
    assert "18" in detail["pending_question"]


def test_notes_lifecycle(client, seeded):
    sid = seeded["rosa"]
    note = client.post(f"/api/bo/candidates/{sid}/notes", json={"body": "Ofrecer puesto de almacén"},
                       headers={"X-Recruiter": "Luis"}).json()
    assert note["author"] == "Luis"
    assert names(search(client, q="almacen")) == ["Rosa Gil Mora"]  # notes are searchable
    assert client.delete(f"/api/bo/candidates/{sid}/notes/{note['id']}").status_code == 204
    assert client.get(f"/api/bo/candidates/{sid}").json()["notes_recruiter"] == []
    assert client.post(f"/api/bo/candidates/{sid}/notes", json={"body": ""}).status_code == 422


def test_edit_fields_validates_and_rescores(client, seeded):
    sid = seeded["carlos"]
    before = client.get(f"/api/bo/candidates/{sid}").json()["score"]
    resp = client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"has_prl_course": True}})
    assert resp.status_code == 200 and resp.json()["changes"]["has_prl_course"] == {"from": False, "to": True}
    after = client.get(f"/api/bo/candidates/{sid}").json()
    assert after["score"] == before + 20
    assert any(e["type"] == "field_edit" for e in after["events"])
    bad = client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"transport": "helicopter"}})
    assert bad.status_code == 422
    assert client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"salary": 1}}).status_code == 422
    city = client.patch(f"/api/bo/candidates/{sid}/data", json={"changes": {"city": "Zaragoza"}}).json()
    assert city["data"]["city"] == "Zaragoza" and city["data"]["in_service_area"] is True


# ---------- manual interaction ----------

def test_takeover_recruiter_conversation_and_handback(client, seeded):
    sid = seeded["ongoing"]
    # recruiter writes: the bot pauses automatically and the message reaches the web chat
    sent = client.post(f"/api/bo/candidates/{sid}/messages", json={"text": "Hola Marta, soy Ana de RRHH."},
                       headers={"X-Recruiter": "Ana"}).json()
    assert sent["bot_paused"] is True and sent["delivered"] is False  # web chat: delivered on next poll
    pending = client.get(f"/api/sessions/{sid}/pending").json()
    assert pending["messages"][0]["role"] == "recruiter" and pending["messages"][0]["author"] == "Ana"
    assert pending["session"]["bot_paused"] is True
    assert client.get(f"/api/sessions/{sid}/pending").json()["messages"] == []  # delivered once

    # candidate answers the recruiter: the agent stays silent
    reply = client.post(f"/api/sessions/{sid}/messages", json={"text": "Hola Ana, ¿a qué hora es la obra?"}).json()
    assert reply["reply"] == "" and reply["paused"] is True and reply["stage"] == "adult"
    board = search(client, q="Marta")["items"][0]
    assert board["awaiting_reply"] is True and board["bot_paused"] is True
    assert search(client, bot_paused="true")["total"] == 1

    # hand back: the agent re-asks the pending question
    back = client.post(f"/api/bo/candidates/{sid}/takeover", json={"paused": False}).json()
    assert back["bot_paused"] is False and "18" in back["message"]
    assert "18" in client.get(f"/api/sessions/{sid}/pending").json()["messages"][0]["content"]
    nxt = client.post(f"/api/sessions/{sid}/messages", json={"text": "sí"}).json()
    assert nxt["stage"] == "work_permit"
    events = [e["type"] for e in client.get(f"/api/bo/candidates/{sid}").json()["events"]]
    assert {"takeover_start", "recruiter_message", "candidate_message_during_takeover", "takeover_end"} <= set(events)


def test_recruiter_can_message_a_closed_candidate(client, seeded):
    sid = seeded["laura"]
    client.post(f"/api/bo/candidates/{sid}/messages", json={"text": "¿Puedes venir el lunes a las 10?"})
    reply = client.post(f"/api/sessions/{sid}/messages", json={"text": "Sí, allí estaré"}).json()
    assert reply["reply"] == "" and reply["status"] == "qualified"  # no canned "we're done" answer


def test_reopen_disqualified_candidate_reasks_the_knockout(client, seeded):
    sid = seeded["pedro"]
    resp = client.post(f"/api/bo/candidates/{sid}/reopen", json={}).json()
    assert resp["status"] == "active" and resp["stage"] == "license"
    after = client.post(f"/api/sessions/{sid}/messages", json={"text": "perdón, sí tengo carnet"}).json()
    assert after["stage"] == "location"
    assert db.get_session(sid)["pipeline_stage"] == "screening"
    assert client.post(f"/api/bo/candidates/{sid}/reopen", json={"question_id": "salary"}).status_code == 422


def test_create_manual_candidate_and_screening_link(client):
    created = client.post("/api/bo/candidates", json={"profile_id": "construction_laborer", "name": "Jorge Blanco Ruiz",
                                                      "contact": "+34 600 000 111", "note": "Referido por obra Getafe"}).json()
    sid = created["id"]
    assert created["chat_link"] == f"/?session={sid}"
    detail = client.get(f"/api/bo/candidates/{sid}").json()
    assert detail["source"] == "backoffice" and detail["data"]["full_name"] == "Jorge Blanco Ruiz"
    assert detail["notes_recruiter"][0]["body"] == "Referido por obra Getafe"
    # the candidate opens the link: the name is already known, so the agent skips that question
    first = client.post(f"/api/sessions/{sid}/messages", json={"text": "hola"}).json()
    assert first["stage"] == "adult"
    assert client.post("/api/bo/candidates", json={"profile_id": "nope"}).status_code == 422


def test_meta_describes_pipeline_profiles_and_flags(client, seeded):
    meta = client.get("/api/bo/meta").json()
    assert [s["id"] for s in meta["pipeline"]][:3] == ["screening", "review", "qualified"]
    assert {p["id"] for p in meta["profiles"]} >= {"delivery_courier", "construction_laborer"}
    assert meta["flags"]["physical_review"] == "Revisar capacidad física"


def test_analytics_per_profile(client, seeded):
    all_profiles = client.get("/api/bo/analytics").json()
    construction = client.get("/api/bo/analytics", params={"profile_id": "construction_laborer"}).json()
    assert all_profiles["total_sessions"] == 5 and construction["total_sessions"] == 3
    assert "work_permit" in construction["funnel"] and "license" not in construction["funnel"]
    assert construction["pipeline"]["review"] == 1
    assert client.get("/api/bo/analytics", params={"profile_id": "nope"}).status_code == 404


# ---------- access control ----------

def test_backoffice_requires_password_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "backoffice_password", "s3cret")
    assert client.get("/api/bo/candidates").status_code == 401
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/backoffice").status_code == 401
    assert client.get("/api/bo/candidates", auth=("recruiter", "wrong")).status_code == 401
    assert client.get("/api/bo/candidates", auth=("recruiter", "s3cret")).status_code == 200
    # the candidate chat keeps working without credentials
    assert client.post("/api/sessions", json={}).status_code == 200


def test_dashboard_redirects_to_backoffice(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/backoffice#/analytics"
