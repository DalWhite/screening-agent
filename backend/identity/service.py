"""Who is who: the candidate behind an application, and what we know about reaching them.

A *candidate* owns applications (sessions) and identity keys: phone numbers
(a line: possession, not a person) and ID documents (keyed hash). Keys link
records; nothing here copies data from one application to another.

Single-instance design: the per-key lock is in-process; the database keeps a
one-live-application-per-process index as the safety net behind it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..storage import db
from .phones import canonical_phone

LIVE = db.LIVE_APPLICATION

_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_guard = threading.Lock()
_secrets: dict[str, bytes] = {}


def lock(key: str) -> threading.RLock:
    """Serialises identity resolution of one key across every channel (WhatsApp, phone, backoffice)."""
    with _guard:
        return _locks[key]


def phone_key(address: Optional[str]) -> Optional[str]:
    return canonical_phone(address)


def candidate_for_phone(key: str, verified: bool, source: Optional[str] = None) -> str:
    return db.candidate_for_key("phone", key, verified, source, settings.phone_key_max_age_days)


def follow_up_days(profile: Any) -> int:
    return getattr(profile, "follow_up_days", None) or settings.follow_up_days


def applications(candidate_id: Optional[str]) -> list[dict[str, Any]]:
    return db.candidate_sessions(db.resolve_candidate(candidate_id)) if candidate_id else []


def live_applications(candidate_id: Optional[str], profile_id: Optional[str] = None,
                      exclude: Optional[str] = None) -> list[dict[str, Any]]:
    return [r for r in applications(candidate_id) if r["status"] in LIVE and r["id"] != exclude
            and (profile_id is None or r["profile_id"] == profile_id)]


# ---------- per-channel facts about a person ----------

def channel_info(candidate_id: Optional[str], channel: str) -> dict[str, Any]:
    return db.candidate_channels(candidate_id).get(channel, {}) if candidate_id else {}


def note_channel(candidate_id: Optional[str], channel: str, **fields: Any) -> None:
    """Remember how to reach the person on ``channel`` (``address``, ``inbound_at``) and which of their
    applications the conversation there is about (``focus``)."""
    if candidate_id:
        db.update_candidate_channel(candidate_id, channel, **fields)


# ---------- identity documents ----------

def _secret() -> bytes:
    if settings.identity_key_secret:
        return settings.identity_key_secret.encode()
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = settings.root_dir / path
    keyfile = path.parent / "identity.key"  # next to the database, never inside it
    cached = _secrets.get(str(keyfile))
    if cached is None:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        if not keyfile.exists():
            keyfile.write_text(secrets.token_hex(32))
            keyfile.chmod(0o600)
        cached = _secrets[str(keyfile)] = keyfile.read_text().strip().encode()
    return cached


def document_key(number: str) -> str:
    """Keyed hash of an ID document number (an unsalted hash of 10^8 DNIs is a dictionary away)."""
    normalised = re.sub(r"[\s.-]", "", number or "").upper()
    return hmac.new(_secret(), normalised.encode(), hashlib.sha256).hexdigest()


@dataclass
class DocumentMatch:
    """The document typed in an application already belongs to another candidate."""

    owner: str  # the candidate who had it: the survivor of the merge
    live_duplicate: Optional[str] = None  # their live application in the same process, if any


def match_document(state: Any, number: str) -> Optional[DocumentMatch]:
    """Record the document as a key of the application's candidate; report when someone else has it."""
    key = document_key(number)
    owner = db.key_owner("id_document", key)
    mine = db.resolve_candidate(state.candidate_id) if state.candidate_id else None
    if owner is None:
        if mine:
            db.add_candidate_key(mine, "id_document", key, verified=False, source="turn")
        return None
    if owner == mine:
        return None
    clash = live_applications(owner, profile_id=state.profile_id, exclude=state.id)
    return DocumentMatch(owner=owner, live_duplicate=clash[0]["id"] if clash else None)


def merge_into(state: Any, owner: str) -> list[str]:
    """The application's candidate becomes ``owner`` (with all their applications and keys)."""
    moved = db.merge_candidates(owner, state.candidate_id) if state.candidate_id else []
    state.candidate_id = owner
    return moved


def recently(ts: Optional[str], days: int) -> bool:
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return False
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when <= timedelta(days=days)
