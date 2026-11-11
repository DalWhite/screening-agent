"""Start the simulator (and optionally the screening app wired to it).

Used by ``python -m simulators`` and by the live e2e fixtures.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Mapping, Optional

import httpx
import uvicorn

from .server import create_app
from .core import ChannelSimulator

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0, proc: Optional[subprocess.Popen] = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"process exited early (code {proc.returncode}) while waiting for {url}")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer within {timeout:.0f}s")


class SimulatorServer:
    """The simulator's HTTP server (REST API + control API + UI) in a background thread."""

    def __init__(self, sim: ChannelSimulator, host: str = "127.0.0.1", port: Optional[int] = None,
                 log_level: str = "warning"):
        self.sim = sim
        self.host = host
        self.port = port or free_port()
        config = uvicorn.Config(create_app(sim), host=host, port=self.port, log_level=log_level,
                                access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="twilio-simulator", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "SimulatorServer":
        self._thread.start()
        wait_http(f"{self.url}/sim/api/health", timeout=15)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def __enter__(self) -> "SimulatorServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


SIM_CONTENT_SID = "HXsimulatedreengagement0000000000"
SIM_META_TEMPLATE = "reengagement"
SIM_TEMPLATE_BODY = "Hola {{1}}, soy {{2}}. ¿Seguimos con tu candidatura? Responde a este mensaje y retomamos."


def app_env(sim: ChannelSimulator, simulator_url: str, app_url: str) -> dict[str, str]:
    """Environment that wires the screening app to the simulator instead of Twilio / Meta / an STT service."""
    install_default_templates(sim)
    return {
        "TWILIO_ACCOUNT_SID": sim.twilio.account_sid,
        "TWILIO_AUTH_TOKEN": sim.twilio.auth_token,
        "TWILIO_API_KEY_SID": "",
        "TWILIO_API_KEY_SECRET": "",
        "TWILIO_WHATSAPP_FROM": f"whatsapp:{sim.whatsapp_number}",
        "TWILIO_WHATSAPP_CONTENT_SID": SIM_CONTENT_SID,
        "TWILIO_API_BASE_URL": simulator_url,
        "TWILIO_VALIDATE_SIGNATURES": "true" if sim.sign else "false",
        "PUBLIC_BASE_URL": app_url,
        "WHATSAPP_PROVIDER": sim.whatsapp_provider,
        "VOICE_PROVIDER": sim.voice_provider,
        "VONAGE_SIGNATURE_SECRET": sim.vonage.signature_secret,
        "VONAGE_VALIDATE_SIGNATURES": "true" if sim.vonage.sign else "false",
        "META_ACCESS_TOKEN": sim.meta.access_token,
        "META_PHONE_NUMBER_ID": sim.meta.phone_number_id,
        "META_APP_SECRET": sim.meta.app_secret,
        "META_VERIFY_TOKEN": sim.meta.verify_token,
        "META_VALIDATE_SIGNATURES": "true" if sim.meta.sign else "false",
        "META_GRAPH_BASE_URL": simulator_url,
        "META_TEMPLATE_NAME": SIM_META_TEMPLATE,
        "STT_PROVIDER": "openai",
        "STT_BASE_URL": f"{simulator_url}/v1",
        "STT_API_KEY": sim.speech.api_key,
    }


def install_default_templates(sim: ChannelSimulator) -> None:
    """The re-engagement template, "approved" on both providers."""
    sim.twilio.templates.setdefault(SIM_CONTENT_SID, SIM_TEMPLATE_BODY)
    sim.meta.templates.setdefault(SIM_META_TEMPLATE, SIM_TEMPLATE_BODY)


class AppProcess:
    """The screening app (uvicorn) as a subprocess with extra environment."""

    def __init__(self, env: Mapping[str, str], port: Optional[int] = None, host: str = "127.0.0.1",
                 log: Optional[IO] = None):
        self.host = host
        self.port = port or free_port()
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), **env}
        self.log = log
        self.proc: Optional[subprocess.Popen] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 30.0) -> "AppProcess":
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", self.host, "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT if self.log else None,
        )
        try:
            wait_http(f"{self.url}/api/health", timeout=timeout, proc=self.proc)
        except RuntimeError:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def kill(self) -> None:
        """Crash: no graceful shutdown (background workers do not finish their tick)."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def restart(self, crash: bool = False, timeout: float = 30.0) -> "AppProcess":
        """Same port, same environment (same database): what a deploy or a crash looks like to Twilio/Meta."""
        self.kill() if crash else self.stop()
        return self.start(timeout=timeout)

    def __enter__(self) -> "AppProcess":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
