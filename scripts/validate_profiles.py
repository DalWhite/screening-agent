"""Validate screening profiles and preview their conversation.

    python scripts/validate_profiles.py                      # validate every profile
    python scripts/validate_profiles.py construction_laborer # + print its question flow and rules
    python scripts/validate_profiles.py construction_laborer --try "hola" "Ana Gil" "sí" ...
                                                             # + run a scripted conversation (MockLLM)

Exit code 1 if any profile is invalid — suitable for CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.config import settings  # noqa: E402
from backend.screening.profile import load_profile, localize  # noqa: E402


def describe(profile) -> None:
    print(f"\n{profile.id} — {localize(profile.role, 'es')} @ {profile.company} (agent {profile.persona.agent_name})")
    for i, q in enumerate(profile.questions, 1):
        req = "required" if q.required else "optional"
        print(f"  {i}. [{q.id}] {q.type:<12} {req:<8} -> {q.field}: {localize(q.prompts, 'es', [''])[0]}")
    for c in profile.checks:
        print(f"  check {c.id:<21} {c.provider:<16} trigger={c.trigger} blocking={c.blocking} -> {c.field}")
    for r in profile.rules:
        trigger = f"{r.field} {r.op} {r.value!r}" if r.field else f"{r.question}:{r.on_error} after {r.after_attempts}"
        print(f"  rule {r.id:<22} {r.action:<10} when {trigger}")
    print(f"  scoring: {len(profile.scoring)} criteria · FAQ: {len(profile.faq)} items · "
          f"areas: {', '.join(profile.areas.cities()) if profile.areas else '—'}")


def try_conversation(profile_id: str, messages: list[str]) -> None:
    from backend.llm.client import MockLLM
    from backend.screening.agent import ScreeningAgent

    agent = ScreeningAgent(MockLLM())
    state = agent.new_session(profile_id=profile_id)
    print(f"\n{agent.profile(state).persona.agent_name}: {agent.opening(state)}")
    for text in messages:
        reply, decision = agent.process(state, text)
        print(f"  candidate: {text}\n{agent.profile(state).persona.agent_name}: {reply}   "
              f"[{decision.stage_before} -> {decision.stage_after}{' · ' + decision.validation_error if decision.validation_error else ''}]")
    print(f"\noutcome={state.stage} flags={state.flags} data={dict(state.data)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile", nargs="?", help="profile id to describe")
    parser.add_argument("--try", dest="messages", nargs="+", help="scripted candidate messages")
    args = parser.parse_args()

    ok = True
    profiles = {}
    for path in sorted(Path(settings.profiles_dir).glob("*/profile.json")):
        try:
            profile = load_profile(path)
            profiles[profile.id] = profile
            print(f"OK    {profile.id:<24} {len(profile.questions)} questions, {len(profile.rules)} rules  ({path})")
        except Exception as exc:  # report every broken profile, not just the first
            ok = False
            print(f"ERROR {path}: {exc}")
    if args.profile:
        if args.profile not in profiles:
            print(f"unknown profile {args.profile!r}")
            return 1
        describe(profiles[args.profile])
        if args.messages:
            try_conversation(args.profile, args.messages)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
