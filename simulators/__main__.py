"""Run the local channel simulator (Twilio WhatsApp + Voice, Meta Cloud API, STT): ``python -m simulators --with-app``.

Without ``--with-app`` it only starts the simulator and prints the ``.env``
values that point an already running (or separately started) app at it.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .runner import AppProcess, SimulatorServer, app_env
from .core import DEFAULT_VOICE_NUMBER, DEFAULT_WHATSAPP_NUMBER, MESSAGING_PROVIDERS, VOICE_PROVIDERS, ChannelSimulator
from .twilio import DEFAULT_ACCOUNT_SID, DEFAULT_AUTH_TOKEN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulators", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900, help="simulator port (UI + fake Twilio API)")
    parser.add_argument("--app-url", default=None, help="URL of the screening app (default: http://HOST:APP_PORT)")
    parser.add_argument("--with-app", action="store_true", help="also start the screening app wired to the simulator")
    parser.add_argument("--app-port", type=int, default=8000)
    parser.add_argument("--db", default="data/simulator.db", help="app database when using --with-app")
    parser.add_argument("--mock-llm", action="store_true", help="run the app with LLM_FALLBACK=mock (no model server)")
    parser.add_argument("--whatsapp-profile", default=None, help="WHATSAPP_PROFILE for the app (--with-app)")
    parser.add_argument("--voice-profile", default=None, help="VOICE_PROFILE for the app (--with-app)")
    parser.add_argument("--account-sid", default=DEFAULT_ACCOUNT_SID)
    parser.add_argument("--auth-token", default=DEFAULT_AUTH_TOKEN)
    parser.add_argument("--whatsapp-number", default=DEFAULT_WHATSAPP_NUMBER)
    parser.add_argument("--voice-number", default=DEFAULT_VOICE_NUMBER)
    parser.add_argument("--no-sign", action="store_true", help="do not sign webhooks (app must disable validation)")
    parser.add_argument("--whatsapp-provider", choices=MESSAGING_PROVIDERS, default="twilio",
                        help="WhatsApp through Twilio (default) or directly through Meta's Cloud API")
    parser.add_argument("--voice-provider", choices=VOICE_PROVIDERS, default="twilio",
                        help="phone line through Twilio (TwiML, default) or Vonage (NCCO)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per webhook is noise; the UI log has them
    app_url = (args.app_url or f"http://{args.host}:{args.app_port}").rstrip("/")
    sim = ChannelSimulator(app_url, twilio={"account_sid": args.account_sid, "auth_token": args.auth_token},
                           whatsapp_number=args.whatsapp_number, voice_number=args.voice_number,
                           sign=not args.no_sign, whatsapp_provider=args.whatsapp_provider,
                           voice_provider=args.voice_provider,
                           graph_url=f"http://{args.host}:{args.port}")
    server = SimulatorServer(sim, host=args.host, port=args.port, log_level="warning").start()
    env = app_env(sim, server.url, app_url)

    app: AppProcess | None = None
    if args.with_app:
        extra = {**env, "DB_PATH": args.db}
        if args.mock_llm:
            extra["LLM_FALLBACK"] = "mock"
        if args.whatsapp_profile is not None:
            extra["WHATSAPP_PROFILE"] = args.whatsapp_profile
        if args.voice_profile is not None:
            extra["VOICE_PROFILE"] = args.voice_profile
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
        app = AppProcess(extra, port=args.app_port, host=args.host).start(timeout=60)
        if args.whatsapp_provider == "meta":
            status, _ = sim.meta.verify_webhook()  # what Meta does when the webhook URL is saved
            print(f"  Meta webhook verification: {'OK' if status == 200 else f'FAILED ({status})'}")

    print()
    print(f"  Channel simulator  {server.url}/            (phones: WhatsApp + calls)")
    if app is not None:
        print(f"  Screening app      {app.url}/backoffice  (recruiter view, db={args.db})")
    else:
        print(f"  Screening app      {app_url}  (not started: run it with these variables)")
        print()
        for key, value in env.items():
            print(f"    {key}={value}")
    print(f"\n  WhatsApp {sim.whatsapp_number} via {args.whatsapp_provider} · voice {sim.voice_number} via "
          f"{args.voice_provider} · "
          f"voice notes transcribed by the simulator · Ctrl+C to stop\n")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            if app is not None and app.proc is not None and app.proc.poll() is not None:
                print("screening app exited", file=sys.stderr)
                return 1
            stop.wait(0.5)
    finally:
        if app is not None:
            app.stop()
        server.stop()
        sim.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
