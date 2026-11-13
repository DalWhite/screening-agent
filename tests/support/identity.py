"""Candidate identity: the contract the identity scenarios are written against.

Today a screening session *is* the candidate: nothing links two sessions of the
same person. The tests under ``tests/*/identity/`` (marker ``identity``) describe
the behaviour we want once candidates have an identity of their own, and most of
them fail until it exists.

They only look at identity through this module, so the final design can store
it however it likes: adapt the helpers here, not the scenarios.

Provisional contract (to confirm in the design step):

* every session row exposes ``candidate_id``, the person it belongs to: sessions
  of the same person share it, different people never do. Until that exists, a
  session counts as its own candidate (which is exactly today's behaviour);
* the analytics expose ``total_candidates`` (people) next to ``total_sessions``;
* the session API accepts an ``Idempotency-Key`` header on
  ``POST /api/sessions`` and ``POST /api/sessions/{id}/messages``, and a
  ``contact`` (phone number) when a session is created.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Iterable, Optional

import pytest

from backend.storage import db

# Scope (docs/identity_design.md §0): one application instance, WhatsApp and phone channels only.
# Scenarios outside it stay written but skipped, so they can be brought back as they are.
DEFERRED_MULTI_WORKER = pytest.mark.skip(
    reason="deferred: several workers/processes are out of scope (single-instance monolith)")
DEFERRED_MONITOR = pytest.mark.skip(
    reason="deferred (W4): single-instance monitor issue, parked with the rest of W4")
DEFERRED_ANALYTICS = pytest.mark.skip(reason="deferred (W5): analytics and retention are out of scope")
DEFERRED_WEB = pytest.mark.skip(
    reason="deferred: the web chat / session API is a test harness; only WhatsApp and phone are in scope")

CANDIDATE_KEY = "candidate_id"
LIVE = ("active", "verifying")

# Openings of the seed processes: a reply that contains one started a new screening.
OPENINGS = {"delivery_courier": "Grupo Sazón", "construction_laborer": "Constructora Horizonte"}


# ---------- who is who ----------

def candidate_of(session_id: str) -> str:
    """The person a session belongs to. Without an identity model, each session is its own candidate."""
    row = db.get_session(session_id)
    assert row is not None, f"no session {session_id}"
    return row.get(CANDIDATE_KEY) or f"session:{session_id}"


def assert_same_candidate(*session_ids: str) -> None:
    people = {sid: candidate_of(sid) for sid in session_ids}
    if len(set(people.values())) > 1:
        linked = any(not p.startswith("session:") for p in people.values())
        hint = "" if linked else f" (sessions carry no {CANDIDATE_KEY!r}: identity resolution is not implemented)"
        raise AssertionError(f"expected one candidate behind these sessions, got {len(set(people.values()))}"
                             f"{hint}: {people}")


def assert_different_candidates(first: str, second: str) -> None:
    assert candidate_of(first) != candidate_of(second), f"{first} and {second} were merged into one candidate"


def total_candidates(analytics: dict[str, Any]) -> int:
    assert "total_candidates" in analytics, (
        "analytics only count sessions (total_sessions): there is no count of people (total_candidates)")
    return analytics["total_candidates"]


# ---------- sessions of a phone number ----------

def sessions_for(*contacts: str, profile_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every session stored with any of these contacts (oldest first), whatever channel opened it."""
    with db.get_conn() as conn:
        marks = ",".join("?" * len(contacts))
        sql = f"SELECT id, profile_id, source, contact, status, stage, started_at FROM sessions WHERE contact IN ({marks})"
        params: list[Any] = list(contacts)
        if profile_id:
            sql += " AND profile_id=?"
            params.append(profile_id)
        return [dict(r) for r in conn.execute(sql + " ORDER BY started_at, rowid", params)]


def live_sessions_for(*contacts: str, profile_id: Optional[str] = None) -> list[dict[str, Any]]:
    return [r for r in sessions_for(*contacts, profile_id=profile_id) if r["status"] in LIVE]


def user_lines(session_id: str) -> list[str]:
    return [m["content"] for m in db.get_session(session_id)["messages"] if m["role"] == "user"]


def event_types(session_id: str) -> list[str]:
    return [e["type"] for e in db.get_session(session_id)["events"]]


def is_opening(text: str, profile_id: Optional[str] = None) -> bool:
    """Did the agent start a screening from scratch (its opening message)?"""
    names = [OPENINGS[profile_id]] if profile_id else list(OPENINGS.values())
    return any(name in (text or "") for name in names)


# ---------- talking to the channels directly (no HTTP) ----------

def send_whatsapp(channel, number: str, *texts: str) -> list[str]:
    """Send WhatsApp messages through a ``MessagingChannel``; returns the replies to the last one."""
    replies: list[str] = []
    for text in texts:
        replies = re.findall(r"<Message>(.*?)</Message>", channel.handle_inbound(f"whatsapp:{number}", text), flags=re.S)
    return replies


def place_call(voice, number: str, call_id: str, *speech: str):
    """A phone call through a ``VoiceChannel``; returns the last ``VoiceAction``."""
    action = voice.start_call(number, call_id)
    for text in speech:
        action = voice.speech(action.session_id, text, call_id=call_id)
    return action


# ---------- scripted candidates (answers after the WhatsApp trigger / the call greeting) ----------

def courier(name: str = "Laura Jiménez Ortiz") -> list[str]:
    return ["sí, empecemos", name, "sí, tengo carnet", "Madrid, Malasaña", "jornada completa", "mañana",
            "2 años en Glovo", "en 2 semanas"]


def courier_no_license(name: str = "Pedro Sánchez Gil") -> list[str]:
    return ["sí, empecemos", name, "no tengo carnet"]


def construction(name: str = "Carlos Martín Ruiz", document: str = "12345678Z") -> list[str]:
    return ["sí", name, "sí, tengo 34 años", "sí, tengo NIE con permiso", f"mi DNI es {document}", "Getafe",
            "sí, sin problema", "no, no lo tengo", "3 años de peón", "tengo coche", "el lunes"]


def construction_underage(name: str = "Lucía Romero Gil") -> list[str]:
    return ["sí", name, "tengo 17 años"]


def text_cells(exclude: Iterable[tuple[str, str]] = ()) -> str:
    """Every text value stored in the database (for privacy checks), minus ``(table, column)`` pairs."""
    skip = set(exclude)
    out: list[str] = []
    with db.get_conn() as conn:
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table}"):
                out.extend(str(row[c]) for c in columns if (table, c) not in skip and isinstance(row[c], str))
    return "\n".join(out)


# ---------- concurrency ----------

def at_once(*jobs: Callable[[], Any]) -> list[Any]:
    """Run the jobs in threads released at the same moment; returns their results in order."""
    barrier = threading.Barrier(len(jobs))
    results: list[Any] = [None] * len(jobs)
    errors: list[BaseException] = []

    def run(index: int, job: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[index] = job()
        except BaseException as exc:  # surfaced to the test below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


class _SlowConnection:
    """A database connection whose reads take ``delay`` seconds, like a networked database does."""

    def __init__(self, conn, delay: float):
        self._conn, self._delay = conn, delay

    def execute(self, sql: str, *args: Any):
        cursor = self._conn.execute(sql, *args)
        if sql.lstrip().upper().startswith("SELECT"):
            time.sleep(self._delay)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> "_SlowConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)


def with_db_latency(monkeypatch, delay: float = 0.005) -> None:
    """Every read waits ``delay`` seconds (SQLite answers in microseconds, Postgres over a network in
    milliseconds): check-then-act races that SQLite hides become visible, deterministically enough
    for a test. Reads outside a transaction hold no lock, so this changes timing, not semantics."""
    real = db.get_conn
    monkeypatch.setattr(db, "get_conn", lambda: _SlowConnection(real(), delay))
