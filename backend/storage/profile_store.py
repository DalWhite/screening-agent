"""Persistence of screening profiles: seed imports, drafts and published revisions.

- ``profiles``: one row per process with its current draft (if any) and the
  number of its latest published revision (0 = never published).
- ``profile_versions``: immutable published revisions. Sessions reference the
  revision that screened them, so old revisions are never deleted while the
  process exists.

Seeds (``backend/data/profiles/*/profile.json``) are imported as revision 1 the
first time they are seen; if a seed file changes later (a code-reviewed change
in the repository), it is imported as a new revision on the next start.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..screening.profile import JobProfile, data_hash
from . import db

log = logging.getLogger("sazon.profile_store")


class DraftConflict(RuntimeError):
    """The draft changed since the editor loaded it (another recruiter saved first)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProfileStore:
    # ---------- registry source ----------

    def sync_seeds(self, seeds: dict[str, dict[str, Any]]) -> None:
        with db.get_conn() as conn:
            for pid, raw in seeds.items():
                seed_hash = data_hash(raw)
                row = conn.execute("SELECT seed_hash, latest_revision FROM profiles WHERE id=?", (pid,)).fetchone()
                if row is not None and row["seed_hash"] == seed_hash:
                    continue
                data = JobProfile.model_validate(raw).to_data()
                if row is None:
                    conn.execute("INSERT INTO profiles (id, created_at, updated_at, latest_revision, seed_hash) "
                                 "VALUES (?,?,?,?,?)", (pid, _now(), _now(), 0, seed_hash))
                    revision = 1
                    note = "Importado desde backend/data/profiles"
                else:
                    revision = row["latest_revision"] + 1
                    note = "Semilla actualizada en el repositorio"
                    log.warning("seed file of %s changed: imported as revision %s", pid, revision)
                self._insert_version(conn, pid, revision, data, "seed", note)
                conn.execute("UPDATE profiles SET seed_hash=? WHERE id=?", (seed_hash, pid))

    def latest_all(self) -> dict[str, tuple[int, dict[str, Any]]]:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT p.id, p.latest_revision, v.data_json FROM profiles p JOIN profile_versions v "
                "ON v.profile_id = p.id AND v.revision = p.latest_revision WHERE p.latest_revision > 0"
            ).fetchall()
        return {r["id"]: (r["latest_revision"], json.loads(r["data_json"])) for r in rows}

    def revision(self, profile_id: str, revision: int) -> Optional[dict[str, Any]]:
        with db.get_conn() as conn:
            row = conn.execute("SELECT data_json FROM profile_versions WHERE profile_id=? AND revision=?",
                               (profile_id, revision)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def stamp(self) -> str:
        """Changes whenever any profile gets a new published revision (cheap cross-process check)."""
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(latest_revision), 0), COALESCE(MAX(updated_at), '') "
                               "FROM profiles").fetchone()
        return f"{row[0]}:{row[1]}:{row[2]}"

    # ---------- editor ----------

    def list(self) -> list[dict[str, Any]]:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.profile_id = p.id) AS sessions "
                "FROM profiles p ORDER BY p.created_at, p.id"
            ).fetchall()
        return [self._summary(r) for r in rows]

    def get(self, profile_id: str) -> Optional[dict[str, Any]]:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.profile_id = p.id) AS sessions "
                "FROM profiles p WHERE p.id=?", (profile_id,)
            ).fetchone()
            if row is None:
                return None
            out = self._summary(row)
            out["draft"] = json.loads(row["draft_json"]) if row["draft_json"] else None
            published = conn.execute("SELECT data_json FROM profile_versions WHERE profile_id=? AND revision=?",
                                     (profile_id, row["latest_revision"])).fetchone()
            out["published"] = json.loads(published["data_json"]) if published else None
            out["versions"] = [dict(v) for v in conn.execute(
                "SELECT revision, published_at, published_by, note, "
                "(SELECT COUNT(*) FROM sessions s WHERE s.profile_id = v.profile_id AND s.profile_revision = v.revision) "
                "AS sessions FROM profile_versions v WHERE profile_id=? ORDER BY revision DESC", (profile_id,))]
        return out

    def create(self, profile_id: str, data: dict[str, Any], by: str) -> None:
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
                raise ValueError(f"ya existe un proceso con id «{profile_id}»")
            conn.execute(
                "INSERT INTO profiles (id, created_at, updated_at, latest_revision, draft_json, draft_revision, "
                "draft_updated_at, draft_updated_by) VALUES (?,?,?,?,?,?,?,?)",
                (profile_id, _now(), _now(), 0, json.dumps(data, ensure_ascii=False), 1, _now(), by),
            )

    def save_draft(self, profile_id: str, data: dict[str, Any], by: str, base_revision: Optional[int]) -> int:
        with db.get_conn() as conn:
            row = conn.execute("SELECT draft_revision, draft_json FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if row is None:
                raise KeyError(profile_id)
            current = row["draft_revision"] if row["draft_json"] else 0
            if base_revision is not None and base_revision != current:
                raise DraftConflict(f"draft is at revision {current}, editor had {base_revision}")
            new = (row["draft_revision"] or 0) + 1
            conn.execute(
                "UPDATE profiles SET draft_json=?, draft_revision=?, draft_updated_at=?, draft_updated_by=?, updated_at=? "
                "WHERE id=?", (json.dumps(data, ensure_ascii=False), new, _now(), by, _now(), profile_id),
            )
            return new

    def discard_draft(self, profile_id: str) -> None:
        with db.get_conn() as conn:
            conn.execute("UPDATE profiles SET draft_json=NULL, draft_updated_at=NULL, draft_updated_by=NULL, updated_at=? "
                         "WHERE id=?", (_now(), profile_id))

    def publish(self, profile_id: str, data: dict[str, Any], by: str, note: Optional[str] = None) -> int:
        with db.get_conn() as conn:
            row = conn.execute("SELECT latest_revision FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if row is None:
                raise KeyError(profile_id)
            revision = row["latest_revision"] + 1
            self._insert_version(conn, profile_id, revision, data, by, note)
            conn.execute("UPDATE profiles SET draft_json=NULL, draft_updated_at=NULL, draft_updated_by=NULL, "
                         "updated_at=? WHERE id=?", (_now(), profile_id))
            return revision

    def delete(self, profile_id: str) -> None:
        with db.get_conn() as conn:
            used = conn.execute("SELECT COUNT(*) FROM sessions WHERE profile_id=?", (profile_id,)).fetchone()[0]
            if used:
                raise ValueError(f"el proceso tiene {used} candidato(s): desactívalo en lugar de borrarlo")
            conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    # ---------- helpers ----------

    @staticmethod
    def _insert_version(conn, profile_id: str, revision: int, data: dict[str, Any], by: str, note: Optional[str]) -> None:
        conn.execute(
            "INSERT INTO profile_versions (profile_id, revision, data_json, published_at, published_by, note) "
            "VALUES (?,?,?,?,?,?)",
            (profile_id, revision, json.dumps(data, ensure_ascii=False), _now(), by, note),
        )
        conn.execute("UPDATE profiles SET latest_revision=?, updated_at=? WHERE id=?", (revision, _now(), profile_id))

    @staticmethod
    def _summary(row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "latest_revision": row["latest_revision"],
            "has_draft": bool(row["draft_json"]),
            "draft_revision": row["draft_revision"] if row["draft_json"] else None,
            "draft_updated_at": row["draft_updated_at"],
            "draft_updated_by": row["draft_updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "sessions": row["sessions"],
        }
