"""Test harness: the screening app + the Twilio simulator, wired together.

``InProcessStack`` boots the real FastAPI app with FastAPI's ``TestClient``
(MockLLM, throwaway SQLite) and plugs the simulator in on both sides:
webhooks go app-wards through the TestClient, and the app's Twilio SDK client
sends its REST calls to the simulator through :class:`SimulatorHttpClient`.
No socket is opened, so it runs in the default ``pytest`` suite.

``LiveStack`` is the same thing over real HTTP (the app as a uvicorn
subprocess, the simulator server in a thread), for browser e2e tests.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import httpx

from .runner import AppProcess, SimulatorServer, app_env, free_port, install_default_templates
from .core import ChannelSimulator, Phone

FINAL_STATUSES = {"qualified", "disqualified", "abandoned"}


class _StackHelpers:
    """Queries shared by both stacks (all through the app's public/recruiter HTTP API)."""

    api: Any  # httpx.Client-like, bound to the app

    def sessions_for(self, phone: Phone | str) -> list[dict[str, Any]]:
        """Every screening session of this phone number, newest first (recruiter API)."""
        number = phone.number if isinstance(phone, Phone) else phone
        rows = self.api.get("/api/sessions", params={"q": number.lstrip("+"), "limit": 50}).json()["sessions"]
        return [r for r in rows if (r.get("contact") or "").replace(" ", "") == number]

    def session_for(self, phone: Phone | str) -> dict[str, Any]:
        rows = self.sessions_for(phone)
        assert rows, f"no session for {phone}"
        return self.session(rows[0]["id"])

    def session(self, session_id: str) -> dict[str, Any]:
        resp = self.api.get(f"/api/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    def candidate(self, session_id: str) -> dict[str, Any]:
        """Backoffice view of a candidate (recruiter fields, timeline)."""
        resp = self.api.get(f"/api/bo/candidates/{session_id}")
        resp.raise_for_status()
        return resp.json()

    def wait_for_status(self, session_id: str, statuses: set[str] | str = FINAL_STATUSES,
                        timeout: float = 10.0) -> dict[str, Any]:
        wanted = {statuses} if isinstance(statuses, str) else set(statuses)
        deadline = time.monotonic() + timeout
        while True:
            session = self.session(session_id)
            if session["status"] in wanted:
                return session
            if time.monotonic() > deadline:
                raise AssertionError(f"session {session_id} is {session['status']!r}, expected {wanted}")
            time.sleep(0.05)


class InProcessStack(_StackHelpers):
    """The app under ``TestClient`` + a simulator. Use as a context manager.

    ``settings`` overrides are applied to ``backend.config.settings`` for the
    lifetime of the stack (the caller is responsible for isolation, e.g. via
    pytest's ``monkeypatch``).
    """

    def __init__(self, settings_overrides: Optional[Mapping[str, Any]] = None, **sim_kwargs: Any):
        self.settings_overrides = dict(settings_overrides or {})
        self.sim_kwargs = sim_kwargs
        self._exit = ExitStack()

    def __enter__(self) -> "InProcessStack":
        from fastapi.testclient import TestClient

        import backend.app as appmod
        from backend.config import settings

        from .server import create_app

        sim = ChannelSimulator("http://testserver", transport=None, graph_url=SIM_HOST, **self.sim_kwargs)
        self._exit.callback(sim.close)
        install_default_templates(sim)
        for key, value in {**app_settings(sim, public_url="http://testserver"), **self.settings_overrides}.items():
            setattr(settings, key, value)
        appmod.store.registry.clear()
        self.client = self._exit.enter_context(TestClient(appmod.app))
        self.client.headers["X-Recruiter"] = "Ana"
        sim.transport.close()
        sim.transport, sim._owns_transport = self.client, False
        # Every provider's outbound HTTP (Twilio SDK, Graph API, media, STT) goes to the simulator's
        # own HTTP app through the hub's single test seam — no sockets, no provider internals.
        sim_http = self._exit.enter_context(TestClient(create_app(sim), base_url=SIM_HOST))
        appmod.hub.use_http(sim_http)
        self.sim, self.app, self.api = sim, appmod, self.client
        return self

    def __exit__(self, *exc) -> None:
        try:
            if exc[0] is None:
                self.sim.flush()
                for channel in self.app.hub.messaging.values():
                    deadline = time.monotonic() + 5
                    while channel._in_flight and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert not channel._in_flight, f"turns never released: {dict(channel._in_flight)}"
        finally:
            self._exit.close()

    # ----- clock control for background jobs -----

    def silence(self, session_id: str, seconds: float) -> None:
        """Pretend the candidate has been silent for ``seconds`` and run the re-engagement monitor once."""
        state = self.app.store.get(session_id)
        state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self.app.monitor.tick()

    def age(self, session_id: str, hours: float) -> None:
        """Move the candidate's last activity ``hours`` into the past (resume-window tests)."""
        from backend.storage import db

        state = self.app.store.get(session_id)
        state.last_candidate_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
        db.save_session(state)


SIM_HOST = "http://simulator.test"  # in-process host name of the simulator's HTTP app


def app_settings(sim: ChannelSimulator, public_url: str) -> dict[str, Any]:
    """``backend.config.settings`` values that point the app at ``sim`` (in-process)."""
    return {
        "llm_fallback": "mock",
        "twilio_account_sid": sim.twilio.account_sid,
        "twilio_auth_token": sim.twilio.auth_token,
        "twilio_api_key_sid": "",
        "twilio_api_key_secret": "",
        "twilio_whatsapp_from": f"whatsapp:{sim.whatsapp_number}",
        "twilio_whatsapp_content_sid": "",
        "twilio_validate_signatures": sim.sign,
        "twilio_api_base_url": SIM_HOST,
        "public_base_url": public_url,
        "whatsapp_provider": sim.whatsapp_provider,
        "voice_provider": sim.voice_provider,
        "vonage_signature_secret": sim.vonage.signature_secret,
        "vonage_validate_signatures": sim.vonage.sign,
        "meta_access_token": sim.meta.access_token,
        "meta_phone_number_id": sim.meta.phone_number_id,
        "meta_app_secret": sim.meta.app_secret,
        "meta_verify_token": sim.meta.verify_token,
        "meta_validate_signatures": sim.meta.sign,
        "meta_graph_base_url": SIM_HOST,
        "meta_template_name": "",
        "stt_provider": "openai",
        "stt_base_url": f"{SIM_HOST}/v1",
        "stt_api_key": sim.speech.api_key,
        "verification_mock_delay_seconds": 0.2,
        "verification_tick_seconds": 0.05,
    }


APP_SETTING_KEYS = tuple(app_settings(ChannelSimulator("http://x", transport=object()), "").keys())


class LiveStack(_StackHelpers):
    """The app as a uvicorn subprocess + the simulator server, over real HTTP."""

    def __init__(self, data_dir: Path, env: Optional[Mapping[str, str]] = None, **sim_kwargs: Any):
        self.data_dir = Path(data_dir)
        self.extra_env = dict(env or {})
        self.sim_kwargs = sim_kwargs
        self._exit = ExitStack()

    def __enter__(self) -> "LiveStack":
        app_port = free_port()
        app_url = f"http://127.0.0.1:{app_port}"
        sim_port = free_port()
        self.sim = ChannelSimulator(app_url, graph_url=f"http://127.0.0.1:{sim_port}", **self.sim_kwargs)
        self._exit.callback(self.sim.close)
        self.sim_server = self._exit.enter_context(SimulatorServer(self.sim, port=sim_port))
        env = {
            **app_env(self.sim, self.sim_server.url, app_url),
            "LLM_FALLBACK": "mock",
            "DB_PATH": str(self.data_dir / "channels.db"),
            "BACKOFFICE_PASSWORD": "",
            "VERIFICATION_MOCK_DELAY_SECONDS": "1",
            "VERIFICATION_TICK_SECONDS": "0.3",
            # keep the silence monitor out of the way unless a test asks for it
            "REENGAGE_FIRST_DELAY_SECONDS": "3600",
            "REENGAGE_SECOND_DELAY_SECONDS": "7200",
            "SESSION_IDLE_SECONDS": "86400",
            **self.extra_env,
        }
        self.log_file = open(self.data_dir / "app.log", "w")
        self._exit.callback(self.log_file.close)
        self.app_process = self._exit.enter_context(AppProcess(env, port=app_port, log=self.log_file))
        self.url = app_url
        self.api = self._exit.enter_context(httpx.Client(base_url=app_url, timeout=15,
                                                         headers={"X-Recruiter": "Ana"}))
        return self

    def __exit__(self, *exc) -> None:
        self._exit.close()

    def restart_app(self, crash: bool = False) -> None:
        """Restart (or crash and restart) the app process; the simulator and the database survive."""
        self.app_process.restart(crash=crash)

    def app_log(self) -> str:
        self.log_file.flush()
        return (self.data_dir / "app.log").read_text(errors="replace")


def iter_replies(phone: Phone, script: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Send a scripted WhatsApp conversation, yielding (message, TwiML replies)."""
    for text in script:
        yield text, phone.whatsapp(text)


# ---------- conversation parity: what the candidate lived vs what the backend recorded ----------

Line = tuple[str, str, str]  # (who: candidate|agent, channel: whatsapp|voice, text)


def conversation_as_lived(phone: Phone) -> list[Line]:
    """Everything the candidate wrote, said, read or heard, on every channel, in order."""
    items: list[tuple[int, Line]] = []
    for m in phone.thread:
        if m.sender == "business":
            if m.delivered:
                items.append((m.seq, ("agent", "whatsapp", m.body)))
        elif m.transcript is not None:
            items.append((m.seq, ("candidate", "whatsapp", m.transcript)))
        elif m.media_id or m.media:
            items.append((m.seq, ("candidate", "whatsapp", m.body or "<media>")))
        else:
            items.append((m.seq, ("candidate", "whatsapp", m.body)))
    for call in phone.calls:
        for u in call.transcript:
            if u.speaker in ("agent", "caller"):
                items.append((u.seq, ("agent" if u.speaker == "agent" else "candidate", "voice", u.text)))
    return [line for _, line in sorted(items)]


def conversation_as_stored(session: dict[str, Any]) -> list[Line]:
    """The backend transcript, normalised to what each channel shows (TTS text, PII masked)."""
    from backend.channels.base import strip_for_voice

    lines: list[Line] = []
    for m in session["messages"]:
        if not m.get("channel"):
            continue  # queued, never delivered
        who = "candidate" if m["role"] == "user" else "agent"
        text = m["content"]
        if m.get("kind") == "media" and not (m.get("meta") or {}).get("caption"):
            text = "<media>"
        if m["channel"] == "voice" and who == "agent":
            text = strip_for_voice(text)
        lines.append((who, m["channel"], text))
    return lines


def redacted(lines: list[Line]) -> list[Line]:
    """Candidate lines as the backend must store them (PII masked, like every channel)."""
    from backend.screening.guardrails import redact_pii, screen_input

    return [(who, ch, redact_pii(screen_input(text).sanitized) if who == "candidate" and text != "<media>" else text)
            for who, ch, text in lines]
