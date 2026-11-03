"""Diagnose the Twilio channel configuration without starting the server.

    python scripts/check_twilio.py                      # credentials + config report
    python scripts/check_twilio.py --send +34600111222  # also send a test WhatsApp message

Checks, in order:
  1. which values the process actually uses and whether .env is shadowed by OS env vars
  2. whether Twilio accepts the credentials (error 20003 = wrong auth token / API key)
  3. trial-account + sender sanity hints
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

from backend.channels.twilio_provider import TwilioProvider  # noqa: E402
from backend.config import settings  # noqa: E402

KEYS = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
        "TWILIO_WHATSAPP_FROM", "TWILIO_VALIDATE_SIGNATURES", "PUBLIC_BASE_URL"]
SECRET = {"TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SECRET"}


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} (len={len(value)})"


def report_sources() -> None:
    env_file = dotenv_values(ROOT / ".env")
    print(f"== values in use (.env: {ROOT / '.env'}) ==")
    for key in KEYS:
        effective = os.getenv(key, "")
        shown = mask(effective) if key in SECRET else (effective or "<empty>")
        from_file = (env_file.get(key) or "").strip()
        note = ""
        if key in os.environ and from_file and os.environ[key].strip() != from_file:
            note = "  <-- OS environment variable SHADOWS the .env value (load_dotenv does not override)"
        elif key in os.environ and key not in env_file:
            note = "  (from OS environment, not .env)"
        if effective != effective.strip():
            note += "  <-- has leading/trailing whitespace"
        print(f"  {key:28s} {shown}{note}")

    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if token.startswith("SK"):
        print("  ! TWILIO_AUTH_TOKEN looks like an API Key SID (SK...). Put the key in TWILIO_API_KEY_SID /"
              " TWILIO_API_KEY_SECRET and the real Auth Token (Console > Account Info) here.")
    elif token and len(token) != 32:
        print(f"  ! Auth tokens are 32 hex chars; this one has {len(token)}. Re-copy it from Console > Account Info.")
    if settings.twilio_whatsapp_from and not settings.twilio_whatsapp_from.startswith("whatsapp:"):
        print("  i TWILIO_WHATSAPP_FROM has no 'whatsapp:' prefix; it is added automatically.")


def check_account(provider: TwilioProvider) -> bool:
    print("\n== credentials check ==")
    if not provider.configured:
        print("  Twilio not configured (need TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN or API key).")
        return False
    print(f"  auth method: {'API key' if provider.uses_api_key else 'auth token'}")
    try:
        info = provider.check_credentials()
    except Exception as exc:  # TwilioRestException or network
        print(f"  FAILED: {exc}")
        print("  -> 20003 means the token/secret does not belong to this Account SID. Open Console > Account Info,"
              " click 'show' on Auth Token, copy it fresh. Make sure it is not the *Test Credentials* token.")
        return False
    print(f"  OK: account '{info['friendly_name']}' status={info['status']} type={info['type']}")
    if str(info["type"]).lower() == "trial":
        print("  i Trial account: WhatsApp works only via the Sandbox (whatsapp:+14155238886) and only to phones"
              " that sent 'join <keyword>' (expires after 72h).")
        if "14155238886" not in settings.twilio_whatsapp_from:
            print(f"  ! TWILIO_WHATSAPP_FROM={settings.twilio_whatsapp_from!r} is not the sandbox number;"
                  " outbound pushes (nudges, late replies) will fail on a trial account.")
    return True


def send_test(provider: TwilioProvider, to: str) -> None:
    print(f"\n== sending free-form test WhatsApp message to {to} ==")
    sid = provider.send_whatsapp(to, "Prueba del agente de screening de Grupo Sazón ✅")
    if sid:
        print(f"  accepted by Twilio, sid={sid}. Check Console > Monitor > Logs > Messaging for delivery status.")
        return
    if provider.template_required:
        print(f"  REFUSED (error {provider.last_error_code}): WhatsApp wants an approved Content Template here.")
        print("  -> If you messaged the sender from your phone in the last 24h, the sender itself is template-only"
              " (new trial senders / pending Meta Business verification). Interactive replies cannot work on it:")
        print("     use the Sandbox (TWILIO_WHATSAPP_FROM=whatsapp:+14155238886, send 'join <keyword>') for dev,"
              " or a Meta-verified sender for production.")
        print("  -> Outside the 24h window this is expected; nudges use TWILIO_WHATSAPP_CONTENT_SID.")
        if settings.twilio_whatsapp_content_sid:
            print(f"\n== retrying with template {settings.twilio_whatsapp_content_sid} ==")
            sid = provider.send_whatsapp(to, content_sid=settings.twilio_whatsapp_content_sid,
                                         content_variables={"1": "hola", "2": "Marisol"})
            print(f"  {'accepted, sid=' + sid if sid else 'FAILED (error ' + str(provider.last_error_code) + ')'}")
        else:
            print("  TWILIO_WHATSAPP_CONTENT_SID is not set, so nudge fallback is unavailable.")
    else:
        print(f"  FAILED (error {provider.last_error_code}); see log output above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", metavar="E164", help="send a test WhatsApp message to this number")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    report_sources()
    provider = TwilioProvider()
    ok = check_account(provider)
    if ok and args.send:
        send_test(provider, args.send)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
