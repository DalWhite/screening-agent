"""Fixtures for the load tests: an isolated server (own port, own database) whose
timings are tuned so that re-engagement and background workers finish within the
test's window, plus the output folder for reports.

Separate from the main e2e ``server`` fixture on purpose: it starts one server per
test module (not once per session) so a stress test never shares a database or a
process with the functional suite, and its env can be tuned independently.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class StressServer:
    url: str
    pid: int
    db_path: Path
    log_path: Path


@pytest.fixture
def stress_server(tmp_path_factory) -> Iterator[StressServer]:
    """Function-scoped on purpose: every scenario gets its own process and database, so
    100-candidate and 20-candidate runs (or two runs of the same scenario) never share data."""
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("stress-data")
    db_path = data_dir / "stress.db"
    env = {
        **os.environ,
        "LLM_FALLBACK": "mock",
        "DB_PATH": str(db_path),
        # short enough that abandoned candidates are nudged and closed inside the test's run,
        # long enough that they don't fire while a candidate is still mid-conversation
        "REENGAGE_FIRST_DELAY_SECONDS": "6",
        "REENGAGE_SECOND_DELAY_SECONDS": "12",
        "SESSION_IDLE_SECONDS": "20",
        "VERIFICATION_MOCK_DELAY_SECONDS": "1",
        "VERIFICATION_TICK_SECONDS": "0.3",
        "BACKOFFICE_PASSWORD": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "PYTHONPATH": str(ROOT),
    }
    log_path = data_dir / "server.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early, see {log_path}")
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("server did not start in 30s")
    yield StressServer(url, proc.pid, db_path, log_path)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def reports_dir() -> Path:
    path = ROOT / "e2e" / "stress" / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path
