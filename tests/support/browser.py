"""Browser helpers for the e2e tests (Playwright)."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def say(page: Page, text: str) -> None:
    """Candidate chat: send a message and wait for the agent's reply bubble."""
    box = page.get_by_label("Tu respuesta")
    expect(box).to_be_enabled()  # the session (and its opening message) is loaded
    agent_bubbles = page.locator(".bubble.agent:not(.typing)")
    before = agent_bubbles.count()
    box.fill(text)
    page.get_by_role("button", name="Enviar").click()
    expect(agent_bubbles).to_have_count(before + 1)
