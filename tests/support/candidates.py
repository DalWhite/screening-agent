"""Candidates for tests: unique names and numbers, scripted conversations, API shortcuts.

Mock ID registry test numbers (checksum-valid): ``VALID_ID`` valid, ``UNKNOWN_ID``
not in the registry, ``ERROR_ID`` registry error, ``SLOW_ID`` valid but 6x slower.
"""

from __future__ import annotations

import random
import string
import time

import httpx


def unique_surname() -> str:
    """Letters only (names are validated), unique per test run."""
    return "".join(random.choice(string.ascii_lowercase) for _ in range(7)).capitalize()


def run_chat(api: httpx.Client, messages: list[str], profile_id: str | None = None,
             language: str | None = None) -> str:
    resp = api.post("/api/sessions", json={"profile_id": profile_id, "language": language})
    resp.raise_for_status()
    sid = resp.json()["session_id"]
    for text in messages:
        api.post(f"/api/sessions/{sid}/messages", json={"text": text}).raise_for_status()
    return sid


VALID_ID = "12345678Z"
UNKNOWN_ID = "99000001C"
ERROR_ID = "98000001Z"
SLOW_ID = "97000001P"

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def unique_dni(prefix: str = "") -> str:
    """A checksum-valid DNI nobody else in the test run has. An ID document identifies a person, so
    candidates that share one are the same candidate (merged; a second live application in the same
    process closes as a duplicate). ``prefix`` picks the mock registry's answer: "" valid, "99" not
    found, "98" registry error, "97" valid but slow."""
    while True:
        number = prefix + "".join(random.choice(string.digits) for _ in range(8 - len(prefix)))
        if prefix or number[:2] not in ("99", "98", "97"):
            return f"{number}{_DNI_LETTERS[int(number) % 23]}"


def construction_qualified(name: str, document: str | None = None) -> list[str]:
    document = document or unique_dni()
    return ["Hola, busco trabajo en obra", name, "sí, tengo 34 años", "sí, tengo NIE con permiso",
            f"mi DNI es {document}", "Getafe", "sí, sin problema", "no, no lo tengo",
            "3 años de peón y algo de albañilería", "tengo coche", "el lunes"]


def construction_id_error(name: str) -> list[str]:
    return construction_qualified(name, ERROR_ID)


def construction_physical(name: str) -> list[str]:
    return ["hola", name, "sí", "sí", "X1234567L", "Sevilla, Dos Hermanas", "no puedo cargar tanto peso", "sí", "0",
            "en autobús", "en 2 semanas"]


def construction_underage(name: str) -> list[str]:
    return ["hola", name, "tengo 17 años"]


def courier_qualified(name: str) -> list[str]:
    return ["hola", name, "sí, tengo carnet", "Madrid, Malasaña", "jornada completa", "flexible",
            "2 años en Glovo", "mañana"]


def courier_no_license(name: str) -> list[str]:
    return ["hola", name, "no tengo carnet"]


def wait_until_decided(api: httpx.Client, session_id: str, timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        session = api.get(f"/api/sessions/{session_id}").json()
        if session["status"] != "verifying":
            return session
        time.sleep(0.2)
    raise AssertionError(f"session {session_id} still verifying after {timeout}s")


def unique_phone() -> str:
    return "+346" + "".join(random.choice(string.digits) for _ in range(8))


def construction_answers(name: str, document: str | None = None) -> list[str]:
    """Answers after the opening: 0 start, 1 name, 2 age, 3 permit, 4 ID, 5 area, 6 physical, 7 PRL,
    8 experience, 9 transport, 10 start date. Each candidate gets their own ID document by default."""
    document = document or unique_dni()
    return ["sí", name, "sí, tengo 34 años", "sí, tengo NIE con permiso", f"mi DNI es {document}", "Getafe",
            "sí, sin problema", "no, no lo tengo", "3 años de peón", "tengo coche", "el lunes"]


def copy_profile(api: httpx.Client, source: str, mutate=None, publish: bool = True) -> str:
    """A private copy of a process for tests that edit rules (other tests keep the seeded ones)."""
    pid = f"{source.split('_')[0]}_{unique_surname().lower()}"
    api.post("/api/bo/profiles", json={"id": pid, "from_profile": source}).raise_for_status()
    if mutate is not None:
        data = api.get(f"/api/bo/profiles/{pid}").json()["working"]
        mutate(data)
        resp = api.put(f"/api/bo/profiles/{pid}/draft", json={"profile": data})
        resp.raise_for_status()
        assert resp.json()["problems"] == [], resp.json()["problems"]
    if publish:
        api.post(f"/api/bo/profiles/{pid}/publish", json={"note": "e2e"}).raise_for_status()
    return pid
