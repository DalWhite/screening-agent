"""REST API for the recruiter backoffice (``/api/bo``)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..analytics import metrics
from ..storage import db
from .auth import require_recruiter
from .profiles_admin import Conflict, ProblemsError, ProfileAdmin
from .service import BackofficeService, Invalid, NotFound

router = APIRouter(prefix="/api/bo", tags=["backoffice"])


def service(request: Request) -> BackofficeService:
    svc = getattr(request.app.state, "backoffice", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="backoffice not ready")
    return svc


def _csv_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def search_filters(
    q: Optional[str] = None,
    profile_id: list[str] = Query(default=[]),
    pipeline_stage: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    source: list[str] = Query(default=[]),
    language: list[str] = Query(default=[]),
    flag: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[]),
    assignee: Optional[str] = None,
    qualified: Optional[bool] = None,
    bot_paused: Optional[bool] = None,
    min_score: Optional[float] = Query(default=None, ge=0, le=100),
    max_score: Optional[float] = Query(default=None, ge=0, le=100),
    started_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}"),
    started_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}"),
    city: Optional[str] = None,
    disqualify_reason: list[str] = Query(default=[]),
    dropped_at: list[str] = Query(default=[]),
    reached: Optional[str] = None,
    faq: Optional[str] = None,
    sort: str = Query(default="last_ts", pattern="^(last_ts|started_at|score|name|city|updated_at)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> db.SearchFilters:
    return db.SearchFilters(
        q=(q or "").strip() or None,
        profile_id=_csv_list(profile_id), pipeline_stage=_csv_list(pipeline_stage), status=_csv_list(status),
        source=_csv_list(source), language=_csv_list(language), flags=_csv_list(flag), tags=_csv_list(tag),
        assignee=assignee or None, qualified=qualified, bot_paused=bot_paused,
        min_score=min_score, max_score=max_score, started_from=started_from, started_to=started_to,
        city=(city or "").strip() or None, disqualify_reason=_csv_list(disqualify_reason),
        dropped_at=_csv_list(dropped_at), reached=reached or None, faq=faq or None,
        sort=sort, order=order, page=page, page_size=page_size,
    )


def admin(request: Request) -> ProfileAdmin:
    svc = getattr(request.app.state, "profiles_admin", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="profile admin not ready")
    return svc


def _guard(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc}") from exc
    except Invalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProblemsError as exc:
        raise HTTPException(status_code=422, detail={"message": "el perfil tiene problemas", "problems": exc.problems}) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------- reads ----------


@router.get("/meta")
def meta(svc: BackofficeService = Depends(service), _: str = Depends(require_recruiter)) -> dict:
    return svc.meta()


@router.get("/candidates")
def candidates(f: db.SearchFilters = Depends(search_filters), svc: BackofficeService = Depends(service),
               _: str = Depends(require_recruiter)) -> dict:
    return svc.search(f)


@router.get("/candidates.csv")
def candidates_csv(f: db.SearchFilters = Depends(search_filters), svc: BackofficeService = Depends(service),
                   _: str = Depends(require_recruiter)) -> Response:
    return Response(
        content=svc.export_csv(f),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="candidatos.csv"'},
    )


@router.get("/pipeline")
def pipeline(f: db.SearchFilters = Depends(search_filters), svc: BackofficeService = Depends(service),
             _: str = Depends(require_recruiter)) -> dict:
    return svc.pipeline(f)


@router.get("/candidates/{session_id}")
def candidate(session_id: str, svc: BackofficeService = Depends(service), _: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.detail, session_id)


@router.get("/analytics")
def analytics(f: db.SearchFilters = Depends(search_filters), _: str = Depends(require_recruiter)) -> dict:
    """Metrics over exactly the candidates the same filters select in the candidate list."""
    profile_id = f.profile_id[0] if len(f.profile_id) == 1 else None
    f.page, f.page_size = 1, 20000
    sessions = db.search_sessions(f, with_facets=False)["items"]
    try:
        return metrics.compute(sessions, profile_id=profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}") from exc


# ---------- writes ----------


class CreateCandidate(BaseModel):
    profile_id: str
    name: Optional[str] = Field(default=None, max_length=100)
    contact: Optional[str] = Field(default=None, max_length=80)
    language: Optional[str] = Field(default=None, pattern="^(es|en)$")
    note: Optional[str] = Field(default=None, max_length=2000)


class UpdateCandidate(BaseModel):
    pipeline_stage: Optional[str] = None
    assignee: Optional[str] = None
    tags: Optional[list[str]] = None
    interview_at: Optional[str] = None


class EditData(BaseModel):
    changes: dict[str, Any]


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    pause_bot: bool = True


class TakeoverIn(BaseModel):
    paused: bool


class ReopenIn(BaseModel):
    question_id: Optional[str] = None


class BulkIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(move|tag|assign)$")
    value: Optional[str] = None


@router.post("/candidates", status_code=201)
def create_candidate(body: CreateCandidate, svc: BackofficeService = Depends(service),
                     by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.create_candidate, body.profile_id, by, name=body.name, contact=body.contact,
                  language=body.language, note=body.note)


@router.post("/candidates/bulk")
def bulk(body: BulkIn, svc: BackofficeService = Depends(service), by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.bulk, body.ids, body.action, body.value, by)


@router.patch("/candidates/{session_id}")
def update_candidate(session_id: str, body: UpdateCandidate, svc: BackofficeService = Depends(service),
                     by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.update, session_id, by, **body.model_dump(exclude_unset=True))


@router.patch("/candidates/{session_id}/data")
def edit_data(session_id: str, body: EditData, svc: BackofficeService = Depends(service),
              by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.edit_fields, session_id, body.changes, by)


@router.post("/candidates/{session_id}/notes", status_code=201)
def add_note(session_id: str, body: NoteIn, svc: BackofficeService = Depends(service),
             by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.add_note, session_id, body.body, by)


@router.delete("/candidates/{session_id}/notes/{note_id}", status_code=204)
def delete_note(session_id: str, note_id: str, svc: BackofficeService = Depends(service),
                by: str = Depends(require_recruiter)) -> Response:
    _guard(svc.delete_note, session_id, note_id, by)
    return Response(status_code=204)


@router.post("/candidates/{session_id}/messages")
def send_message(session_id: str, body: MessageIn, svc: BackofficeService = Depends(service),
                 by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.send_message, session_id, body.text, by, pause_bot=body.pause_bot)


@router.post("/candidates/{session_id}/takeover")
def takeover(session_id: str, body: TakeoverIn, svc: BackofficeService = Depends(service),
             by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.takeover, session_id, body.paused, by)


@router.post("/candidates/{session_id}/reopen")
def reopen(session_id: str, body: ReopenIn, svc: BackofficeService = Depends(service),
           by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.reopen, session_id, by, question_id=body.question_id)


# ---------- external checks ----------


class VerificationDecisionIn(BaseModel):
    status: str = Field(pattern="^(valid|invalid)$")
    note: Optional[str] = Field(default=None, max_length=300)


@router.post("/candidates/{session_id}/verifications/{verification_id}/decision")
def decide_verification(session_id: str, verification_id: str, body: VerificationDecisionIn,
                        svc: BackofficeService = Depends(service), by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.decide_verification, session_id, verification_id, body.status, body.note, by)


@router.post("/candidates/{session_id}/verifications/{verification_id}/retry")
def retry_verification(session_id: str, verification_id: str, svc: BackofficeService = Depends(service),
                       by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.retry_verification, session_id, verification_id, by)


# ---------- recruiting processes (screening profiles) ----------


class CreateProfileIn(BaseModel):
    id: str = Field(min_length=3, max_length=41)
    company: str = Field(default="", max_length=120)
    role: str = Field(default="", max_length=120)
    from_profile: Optional[str] = None


class DraftIn(BaseModel):
    profile: dict[str, Any]
    base_revision: Optional[int] = None


class PublishIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class ActiveIn(BaseModel):
    active: bool


class SimulateIn(BaseModel):
    messages: list[str] = Field(default_factory=list, max_length=40)
    source: str = Field(default="draft", pattern="^(draft|published)$")
    language: Optional[str] = Field(default=None, pattern="^(es|en)$")


@router.get("/profiles")
def list_profiles_admin(adm: ProfileAdmin = Depends(admin), _: str = Depends(require_recruiter)) -> dict:
    return {"profiles": adm.list()}


@router.post("/profiles", status_code=201)
def create_profile(body: CreateProfileIn, adm: ProfileAdmin = Depends(admin), by: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.create, body.id, by, body.company.strip(), body.role.strip(), body.from_profile)


@router.get("/profiles/{profile_id}")
def get_profile_admin(profile_id: str, adm: ProfileAdmin = Depends(admin), _: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.get, profile_id)


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: str, adm: ProfileAdmin = Depends(admin), _: str = Depends(require_recruiter)) -> Response:
    _guard(adm.delete, profile_id)
    return Response(status_code=204)


@router.put("/profiles/{profile_id}/draft")
def save_draft(profile_id: str, body: DraftIn, adm: ProfileAdmin = Depends(admin),
               by: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.save_draft, profile_id, body.profile, by, body.base_revision)


@router.delete("/profiles/{profile_id}/draft", status_code=204)
def discard_draft(profile_id: str, adm: ProfileAdmin = Depends(admin), _: str = Depends(require_recruiter)) -> Response:
    _guard(adm.discard_draft, profile_id)
    return Response(status_code=204)


@router.post("/profiles/{profile_id}/publish")
def publish_profile(profile_id: str, body: PublishIn, adm: ProfileAdmin = Depends(admin),
                    by: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.publish, profile_id, by, body.note)


@router.post("/profiles/{profile_id}/active")
def set_profile_active(profile_id: str, body: ActiveIn, adm: ProfileAdmin = Depends(admin),
                       by: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.set_active, profile_id, body.active, by)


@router.get("/profiles/{profile_id}/versions/{revision}")
def get_profile_version(profile_id: str, revision: int, adm: ProfileAdmin = Depends(admin),
                        _: str = Depends(require_recruiter)) -> Response:
    data = _guard(adm.version, profile_id, revision)
    import json as _json

    return Response(_json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{profile_id}-r{revision}.json"'})


@router.post("/profiles/{profile_id}/versions/{revision}/restore")
def restore_profile_version(profile_id: str, revision: int, adm: ProfileAdmin = Depends(admin),
                            by: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.restore, profile_id, revision, by)


@router.post("/profiles/{profile_id}/simulate")
def simulate_profile(profile_id: str, body: SimulateIn, adm: ProfileAdmin = Depends(admin),
                     _: str = Depends(require_recruiter)) -> dict:
    return _guard(adm.simulate, profile_id, body.messages, body.source, body.language)


# ---------- configurable pipeline stages ----------


class StageIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    color: str = "indigo"
    after: Optional[str] = None


class StagePatch(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=40)
    color: Optional[str] = None


class StageOrder(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)


@router.get("/stages")
def list_stages(svc: BackofficeService = Depends(service), _: str = Depends(require_recruiter)) -> dict:
    return {"stages": svc.stages()}


@router.post("/stages", status_code=201)
def create_stage(body: StageIn, svc: BackofficeService = Depends(service), by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.create_stage, body.label, body.color, body.after, by)


@router.patch("/stages/{stage_id}")
def update_stage(stage_id: str, body: StagePatch, svc: BackofficeService = Depends(service),
                 by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.update_stage, stage_id, by, label=body.label, color=body.color)


@router.put("/stages/order")
def reorder_stages(body: StageOrder, svc: BackofficeService = Depends(service), by: str = Depends(require_recruiter)) -> dict:
    return {"stages": _guard(svc.reorder_stages, body.ids, by)}


@router.delete("/stages/{stage_id}")
def delete_stage(stage_id: str, move_to: Optional[str] = None, svc: BackofficeService = Depends(service),
                 by: str = Depends(require_recruiter)) -> dict:
    return _guard(svc.delete_stage, stage_id, move_to, by)
