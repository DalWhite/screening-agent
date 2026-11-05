"""Verification providers: who answers an external check.

- ``mock_id_registry``: local stand-in for an identity registry (demo, tests).
  It answers asynchronously after ``VERIFICATION_MOCK_DELAY_SECONDS``. Test
  numbers, like payment test cards: document numbers whose digits start with
  ``99`` are *invalid*, ``98`` → provider *error*, ``97`` → valid but slow
  (6× the delay); anything else is *valid*.
- ``webhook``: POSTs the request to an external service, which answers later on
  ``POST /api/verifications/{id}/callback`` (HMAC-signed).
- ``manual``: nobody is called; a recruiter decides in the backoffice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from ..config import settings


class ProviderError(RuntimeError):
    """Transient failure talking to a provider: the dispatch is retried."""


@dataclass
class Submission:
    status: str = "submitted"          # submitted | valid | invalid | error
    due_at: Optional[datetime] = None  # when to poll (mock)
    reference: Optional[str] = None    # provider-side id
    detail: Optional[str] = None


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_ok(body: bytes, header: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign(body, secret), header or "")


class Provider:
    name = "base"

    def submit(self, verification: dict[str, Any], check: Any, payload: dict[str, Any], now: datetime) -> Submission:
        raise NotImplementedError

    def poll(self, verification: dict[str, Any], payload: dict[str, Any], now: datetime) -> Optional[tuple[str, str]]:
        """(status, detail) once the provider has an answer, else None."""
        return None


class MockIdRegistry(Provider):
    name = "mock_id_registry"

    def __init__(self, delay_seconds: Optional[float] = None):
        self._delay = delay_seconds

    @property
    def delay(self) -> float:
        return settings.verification_mock_delay_seconds if self._delay is None else self._delay

    @staticmethod
    def _digits(payload: dict[str, Any]) -> str:
        for value in payload.values():
            digits = re.sub(r"\D", "", str(value or ""))
            if len(digits) >= 6:
                return digits
        return ""

    def submit(self, verification, check, payload, now):
        slow = self._digits(payload).startswith("97")
        return Submission(due_at=now + timedelta(seconds=self.delay * (6 if slow else 1)),
                          reference=f"MOCK-{verification['id'][:8]}")

    def poll(self, verification, payload, now):
        due = verification.get("due_at")
        if due and now < datetime.fromisoformat(due):
            return None
        digits = self._digits(payload)
        if not digits:
            return "error", "No se recibió ningún número de documento"
        if digits.startswith("99"):
            return "invalid", "El documento no consta en el registro"
        if digits.startswith("98"):
            return "error", "Registro no disponible"
        return "valid", "Documento verificado en el registro"


class ManualProvider(Provider):
    name = "manual"

    def submit(self, verification, check, payload, now):
        return Submission(detail="Pendiente de revisión por un reclutador")


class WebhookProvider(Provider):
    name = "webhook"

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client

    def submit(self, verification, check, payload, now):
        url = getattr(check, "url", None) or settings.verification_webhook_url
        if not url:
            raise ProviderError("no webhook URL configured (check.url or VERIFICATION_WEBHOOK_URL)")
        if not webhook_url_allowed(url):
            # never send candidate data to a destination operators did not allow
            return Submission(status="error", detail="destino no permitido (VERIFICATION_ALLOWED_HOSTS)")
        base = settings.public_base_url or ""
        body = json.dumps({
            "verification_id": verification["id"],
            "check": verification["check_id"],
            "payload": payload,
            "deadline": verification["deadline_at"],
            "callback_url": f"{base}/api/verifications/{verification['id']}/callback",
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if settings.verification_webhook_secret:
            headers["X-Signature"] = sign(body, settings.verification_webhook_secret)
        client = self.client or httpx.Client(timeout=10)
        try:
            resp = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc
        finally:
            if self.client is None:
                client.close()
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ProviderError(f"provider answered {resp.status_code}")
        if resp.status_code >= 400:
            return Submission(status="error", detail=f"request rejected by provider ({resp.status_code})")
        data = resp.json() if resp.content else {}
        status = data.get("status")
        if status in ("valid", "invalid", "error"):  # some providers answer synchronously
            return Submission(status=status, reference=data.get("reference"), detail=data.get("detail"))
        return Submission(reference=data.get("reference"))


def webhook_url_allowed(url: str) -> bool:
    """Only HTTPS hosts configured by operators (plain HTTP for localhost during development)."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    allowed = {h.strip().lower() for h in settings.verification_allowed_hosts.split(",") if h.strip()}
    default_host = urlparse(settings.verification_webhook_url).hostname
    if default_host:
        allowed.add(default_host.lower())
    local = host in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        return False
    return host in allowed


def default_providers() -> dict[str, Provider]:
    return {p.name: p for p in (MockIdRegistry(), ManualProvider(), WebhookProvider())}
