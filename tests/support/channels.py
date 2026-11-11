"""Channel stacks for the e2e tests: the app + the channel simulator over real HTTP.

A stack is the app (uvicorn subprocess, MockLLM, its own SQLite) and the
simulator (Twilio, Meta, Vonage, speech-to-text) talking over real HTTP.
`StackPool` starts one per configuration on first use and shares it for the
session: ``stacks.get("meta", voice="vonage", SESSION_IDLE_SECONDS="8")``.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

CONSTRUCTION = {"WHATSAPP_PROFILE": "construction_laborer", "VOICE_PROFILE": "construction_laborer"}


class StackPool:
    def __init__(self, tmp_path_factory):
        self._tmp = tmp_path_factory
        self._stacks: dict[tuple, object] = {}

    def get(self, provider: str = "twilio", sim_kwargs: dict | None = None, voice: str = "twilio", **env: str):
        """A running stack for this configuration (WhatsApp ``provider``, ``voice`` provider, app
        ``env``), with the simulator's faults reset."""
        from simulators.testing import LiveStack
        from simulators.core import Faults

        key = (provider, voice, tuple(sorted(env.items())), tuple(sorted((sim_kwargs or {}).items())))
        stack = self._stacks.get(key)
        if stack is None:
            data_dir = self._tmp.mktemp(f"channels-{provider}-{voice}")
            stack = LiveStack(data_dir, env=env, whatsapp_provider=provider, voice_provider=voice,
                              **(sim_kwargs or {})).__enter__()
            self._stacks[key] = stack
        stack.sim.flush(timeout=5)
        stack.sim.faults = Faults()
        stack.sim.transcriber = None
        return stack

    def close(self) -> None:
        for stack in self._stacks.values():
            stack.__exit__(None, None, None)


def only_session(stack, phone) -> dict:
    rows = stack.sessions_for(phone)
    assert len(rows) == 1, [(r["source"], r["status"]) for r in rows]
    return stack.session(rows[0]["id"])


def assert_nothing_lost(stack, phone, timeout: float = 5.0, ordered: bool = True) -> dict:
    """The backend transcript equals what the candidate lived, line for line, on every channel.

    A push lands on the phone a moment before the app settles its row (outbox), so the two
    views are compared until they agree (or ``timeout``). ``ordered=False`` compares the lines
    in any order: under heavy concurrency the simulator's threads may record a Twilio TwiML
    reply and an API message sent right after it in either order (ordering is pinned by
    tests/integration/channels/test_robustness.py)."""
    from collections import Counter

    from simulators.testing import conversation_as_lived, conversation_as_stored, redacted

    view = (lambda lines: lines) if ordered else Counter
    deadline = time.time() + timeout
    while True:
        stack.sim.flush()
        session = only_session(stack, phone)
        stored, lived = view(conversation_as_stored(session)), view(redacted(conversation_as_lived(phone)))
        if stored == lived or time.time() > deadline:
            break
        time.sleep(0.1)
    assert stored == lived
    return session


def open_candidate(page: Page, stack, session_id: str, tab: str = "conversation") -> None:
    page.goto(f"{stack.url}/backoffice#/candidates/{session_id}?tab={tab}")
    expect(page.get_by_test_id("candidate-name")).to_be_visible()


def ensure_profile(stack, profile_id: str, source: str, mutate) -> str:
    """A published copy of ``source`` with ``mutate`` applied, created once per stack."""
    if stack.api.get(f"/api/bo/profiles/{profile_id}").status_code == 200:
        return profile_id
    stack.api.post("/api/bo/profiles", json={"id": profile_id, "from_profile": source}).raise_for_status()
    data = stack.api.get(f"/api/bo/profiles/{profile_id}").json()["working"]
    mutate(data)
    resp = stack.api.put(f"/api/bo/profiles/{profile_id}/draft", json={"profile": data})
    resp.raise_for_status()
    assert resp.json()["problems"] == [], resp.json()["problems"]
    stack.api.post(f"/api/bo/profiles/{profile_id}/publish", json={"note": "e2e"}).raise_for_status()
    return profile_id
