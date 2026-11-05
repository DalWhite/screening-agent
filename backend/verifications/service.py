"""Asynchronous external checks (outbox + worker).

A candidate turn never waits for an external system: when the agent requests a
check, a ``queued`` row is stored together with its payload. A background
worker dispatches queued rows to their provider, polls providers that answer
later, retries transient failures and enforces deadlines. Every final answer
(``valid`` / ``invalid`` / ``error``) is applied to the live session through
``ScreeningAgent.apply_check_result``, which re-runs the profile's rules: a
knockout closes the screening, a blocking check that was holding the decision
releases it, and the candidate is told on their channel.

Privacy: the payload (e.g. a full ID number) is the only copy of sensitive
values. It is deleted as soon as the check has a definitive answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..config import settings
from ..screening.agent import ScreeningAgent, SessionState
from ..screening.schema import TurnDecision
from ..session_store import SessionStore
from ..storage import db
from .providers import Provider, ProviderError, default_providers

log = logging.getLogger("sazon.verifications")

OPEN = ("queued", "submitted")
FINAL = ("valid", "invalid", "error")

Notifier = Callable[[SessionState, str], bool]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


class VerificationError(ValueError):
    pass


class VerificationService:
    def __init__(self, agent: ScreeningAgent, store: SessionStore, notifier: Optional[Notifier] = None,
                 providers: Optional[dict[str, Provider]] = None, clock: Callable[[], datetime] = _now):
        self.agent = agent
        self.store = store
        self.notifier = notifier
        self.providers = providers or default_providers()
        self.clock = clock
        self._task: Optional[asyncio.Task] = None

    # ---------- requests (called in the candidate's turn: no network) ----------

    def request_from_decision(self, state: SessionState, decision: TurnDecision) -> list[str]:
        return [self.request(state, r["check"], r.get("payload") or {}) for r in decision.check_requests]

    def request(self, state: SessionState, check_id: str, payload: dict[str, Any]) -> str:
        check = self.agent.profile(state).check(check_id)
        if check is None:
            raise VerificationError(f"unknown check {check_id!r}")
        now = self.clock()
        vid = uuid.uuid4().hex
        with db.get_conn() as conn:
            # a new answer supersedes any verification still running for the same check
            conn.execute(
                "UPDATE verifications SET status='superseded', payload_json=NULL, updated_at=? "
                "WHERE session_id=? AND check_id=? AND status IN ('queued','submitted')",
                (_iso(now), state.id, check_id),
            )
            conn.execute(
                "INSERT INTO verifications (id, session_id, check_id, provider, status, payload_json, attempts, "
                "created_at, updated_at, due_at, deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vid, state.id, check_id, check.provider, "queued", json.dumps(payload, ensure_ascii=False), 0,
                 _iso(now), _iso(now), _iso(now), _iso(now + timedelta(seconds=check.timeout_seconds))),
            )
        return vid

    # ---------- worker ----------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.verification_tick_seconds)
            try:
                # provider calls may block (HTTP): keep the event loop free
                await asyncio.get_running_loop().run_in_executor(None, self.tick)
            except Exception:
                log.exception("verification tick failed")

    def tick(self) -> None:
        now = self.clock()
        with db.get_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM verifications WHERE status IN ('queued','submitted') ORDER BY created_at"
            )]
        for row in rows:
            try:
                self._advance(row, now)
            except Exception:
                log.exception("verification %s failed to advance", row["id"])

    def _advance(self, row: dict[str, Any], now: datetime) -> None:
        if now >= datetime.fromisoformat(row["deadline_at"]):
            self.complete(row["id"], "error", "Sin respuesta del proveedor antes del plazo", by="timeout")
            return
        provider = self.providers.get(row["provider"])
        if provider is None:
            self.complete(row["id"], "error", f"Proveedor desconocido: {row['provider']}")
            return
        payload = json.loads(row["payload_json"] or "{}")
        if row["status"] == "queued":
            if row["due_at"] and now < datetime.fromisoformat(row["due_at"]):
                return  # waiting for a retry backoff
            self._dispatch(row, provider, payload, now)
            return
        result = provider.poll(row, payload, now)
        if result is not None:
            self.complete(row["id"], result[0], result[1])

    def _dispatch(self, row: dict[str, Any], provider: Provider, payload: dict[str, Any], now: datetime) -> None:
        state = self.store.get(row["session_id"])
        check = self.agent.profile(state).check(row["check_id"]) if state else None
        attempts = row["attempts"] + 1
        try:
            sub = provider.submit(row, check, payload, now)
        except ProviderError as exc:
            if attempts >= settings.verification_max_attempts:
                self._update(row["id"], attempts=attempts)
                self.complete(row["id"], "error", f"Proveedor no disponible: {exc}")
                return
            backoff = now + timedelta(seconds=5 * 2 ** attempts)
            self._update(row["id"], attempts=attempts, due_at=_iso(backoff), result_detail=str(exc)[:300])
            log.warning("verification %s dispatch failed (attempt %s): %s", row["id"], attempts, exc)
            return
        self._update(row["id"], attempts=attempts, status="submitted", due_at=_iso(sub.due_at),
                     reference=sub.reference, result_detail=sub.detail)
        if sub.status in FINAL:
            self.complete(row["id"], sub.status, sub.detail)

    # ---------- results ----------

    def complete(self, verification_id: str, status: str, detail: Optional[str] = None, by: Optional[str] = None,
                 reference: Optional[str] = None) -> dict[str, Any]:
        """Record a final answer (provider callback, poll, timeout or recruiter decision) and apply it."""
        if status not in FINAL:
            raise VerificationError(f"invalid status {status!r}")
        row = self.get(verification_id, with_payload=True)
        if row is None:
            raise LookupError(verification_id)
        if row["status"] == "superseded":
            raise VerificationError("this verification was replaced by a newer one")
        now = self.clock()
        # the payload is kept only while a retry may still need it (errors: provider down, timeout)
        keep_payload = status == "error"
        self._update(verification_id, status=status, result_detail=(detail or "")[:300] or None,
                     completed_at=_iso(now), decided_by=by, reference=reference or row["reference"],
                     payload_json=row["payload_json"] if keep_payload else None)
        with self.store.lock(row["session_id"]):
            state = self.store.get(row["session_id"])
            if state is not None:
                reply, decision = self.agent.apply_check_result(state, row["check_id"], status, detail)
                for event in decision.events:
                    if event["type"] == "check_result" and by:
                        event["payload"]["by"] = by
                db.save_session(state)
                if reply:
                    delivered = self._push(state, reply)
                    db.append_message(state.id, "assistant", reply, state.stage, delivered=delivered)
                db.append_events(state.id, decision.events, state.stage)
        log.info("verification %s (%s) -> %s", verification_id, row["check_id"], status)
        return self.get(verification_id)

    def retry(self, verification_id: str) -> str:
        row = self.get(verification_id, with_payload=True)
        if row is None:
            raise LookupError(verification_id)
        if row["status"] != "error" or not row["payload_json"]:
            raise VerificationError("only failed verifications that still have their data can be retried")
        state = self.store.get(row["session_id"])
        if state is None:
            raise LookupError(row["session_id"])
        with self.store.lock(state.id):
            vid = self.request(state, row["check_id"], json.loads(row["payload_json"]))
            self._update(verification_id, payload_json=None)
            check = self.agent.profile(state).check(row["check_id"])
            state.data[check.field] = "pending"
            state.flags = [f for f in state.flags if f != f"check_error:{row['check_id']}"]
            db.save_session(state)
            db.append_events(state.id, [{"type": "check_requested", "payload": {"check": row["check_id"],
                                                                                "provider": row["provider"],
                                                                                "retry_of": verification_id}}],
                             state.stage)
        return vid

    # ---------- reads ----------

    def get(self, verification_id: str, with_payload: bool = False) -> Optional[dict[str, Any]]:
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM verifications WHERE id=?", (verification_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        if not with_payload:
            out["has_payload"] = bool(out.pop("payload_json"))
        return out

    def for_session(self, session_id: str) -> list[dict[str, Any]]:
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM verifications WHERE session_id=? ORDER BY created_at, rowid",
                                (session_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["has_payload"] = bool(item.pop("payload_json"))
            out.append(item)
        return out

    # ---------- helpers ----------

    def _push(self, state: SessionState, text: str) -> bool:
        if self.notifier is None:
            return False
        try:
            return bool(self.notifier(state, text))
        except Exception:
            log.exception("push after verification failed for session %s", state.id)
            return False

    def _update(self, verification_id: str, **values: Any) -> None:
        values["updated_at"] = _iso(self.clock())
        cols = ", ".join(f"{k}=?" for k in values)
        with db.get_conn() as conn:
            conn.execute(f"UPDATE verifications SET {cols} WHERE id=?", (*values.values(), verification_id))
