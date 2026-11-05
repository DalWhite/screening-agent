"""Playwright end-to-end fixtures.

A real uvicorn server is started once per test session with the deterministic
MockLLM (no model server needed) and a throwaway SQLite database. Tests talk to
it through a real Chromium browser; data is seeded through the public chat API,
exactly as a candidate would create it. Every test uses unique candidate names
so tests stay independent while sharing one server.

Run:  python -m pytest e2e            (add --headed / --slowmo 300 to watch)
"""

from __future__ import annotations

import os
import random
import socket
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

ROOT = Path(__file__).resolve().parent.parent
RECRUITER = "Ana Ruiz"

expect.set_options(timeout=10_000)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory) -> Iterator[str]:
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("e2e-data")
    env = {
        **os.environ,
        "LLM_FALLBACK": "mock",
        "DB_PATH": str(data_dir / "e2e.db"),
        # keep the silence monitor out of the way: tests control every message
        "REENGAGE_FIRST_DELAY_SECONDS": "3600",
        "REENGAGE_SECOND_DELAY_SECONDS": "7200",
        "SESSION_IDLE_SECONDS": "86400",
        # external checks: the mock ID registry answers after 1s (test numbers 97… take 6s)
        "VERIFICATION_MOCK_DELAY_SECONDS": "1",
        "VERIFICATION_TICK_SECONDS": "0.3",
        "BACKOFFICE_PASSWORD": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "PYTHONPATH": str(ROOT),
    }
    log = open(data_dir / "server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early, see {data_dir / 'server.log'}")
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("server did not start in 30s")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def base_url(server: str) -> str:
    """pytest-playwright resolves relative page.goto() URLs against this."""
    return server


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}, "locale": "es-ES",
            "accept_downloads": True}


@pytest.fixture
def context(context: BrowserContext) -> BrowserContext:
    """pytest-playwright's context (keeps --tracing/--video), with the recruiter's name pre-set."""
    context.add_init_script(f"localStorage.setItem('bo_recruiter_name', '{RECRUITER}')")
    return context


@pytest.fixture
def candidate_page(new_context) -> Page:
    """A second, independent browser (the candidate's phone) for two-sided scenarios."""
    page = new_context().new_page()
    page.set_viewport_size({"width": 420, "height": 860})
    return page


@pytest.fixture(scope="session")
def api(server: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server, timeout=10, headers={"X-Recruiter": "seed"}) as client:
        yield client


# ---------- UI helpers ----------

def say(page: Page, text: str) -> None:
    """Candidate chat: send a message and wait for the agent's reply bubble."""
    box = page.get_by_label("Tu respuesta")
    expect(box).to_be_enabled()  # the session (and its opening message) is loaded
    agent_bubbles = page.locator(".bubble.agent:not(.typing)")
    before = agent_bubbles.count()
    box.fill(text)
    page.get_by_role("button", name="Enviar").click()
    expect(agent_bubbles).to_have_count(before + 1)


# ---------- data helpers ----------

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


# Mock ID registry test numbers (checksum-valid): valid / not in the registry / registry error / slow
VALID_ID = "12345678Z"
UNKNOWN_ID = "99000001C"
ERROR_ID = "98000001Z"
SLOW_ID = "97000001P"


def construction_qualified(name: str, document: str = VALID_ID) -> list[str]:
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


@pytest.fixture
def seed(api: httpx.Client):
    """Create candidates through the chat API; returns {label: (session_id, full_name)}."""

    def _seed(**scripts) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for label, (builder, first, profile_id) in scripts.items():
            name = f"{first} {unique_surname()}"
            out[label] = (run_chat(api, builder(name), profile_id), name)
        for sid, _ in out.values():  # external checks answer asynchronously: wait for the final decisions
            wait_until_decided(api, sid)
        return out

    return _seed


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
