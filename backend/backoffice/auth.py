"""Backoffice access control.

Pilot-grade protection: HTTP Basic auth when BACKOFFICE_PASSWORD is set
(put the portal behind SSO / a reverse proxy in production). Without a
password the portal is open, which is only meant for local development.

The acting recruiter's display name (stored in audit events and notes) comes
from the ``X-Recruiter`` header sent by the portal, falling back to the
Basic-auth user.
"""

from __future__ import annotations

import base64
import secrets
from urllib.parse import unquote

from fastapi import HTTPException, Request

from ..config import settings


def _basic_user(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    ok_user = secrets.compare_digest(user, settings.backoffice_user)
    ok_pass = secrets.compare_digest(password, settings.backoffice_password)
    return user if ok_user and ok_pass else None


def require_recruiter(request: Request) -> str:
    """FastAPI dependency: authorise the request and return the acting recruiter's name."""
    user = None
    if settings.backoffice_password:
        user = _basic_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required",
                                headers={"WWW-Authenticate": 'Basic realm="backoffice"'})
    name = unquote(request.headers.get("X-Recruiter", "")).strip()[:60]
    return name or user or "recruiter"
