"""Playwright end-to-end fixtures.

A real uvicorn server is started once per test session with the deterministic
MockLLM (no model server needed) and a throwaway SQLite database. Tests talk to
it through a real Chromium browser; data is seeded through the public chat API,
exactly as a candidate would create it. Every test uses unique candidate names
so tests stay independent while sharing one server.

Run:  python -m pytest -m e2e            (add --headed / --slowmo 300 to watch)

Helpers (candidate scripts, browser helpers, channel stacks) live in ``tests/support/``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Iterator

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.support.candidates import run_chat, unique_surname, wait_until_decided
from tests.support.paths import REPO

ROOT = REPO
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
