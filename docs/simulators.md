# Local channel simulator (WhatsApp via Twilio or Meta, voice via Twilio or Vonage, voice notes)

`simulators/` stands in for the platforms the channels depend on, so WhatsApp
and phone screenings run end to end on a laptop. You don't need a Twilio or
Meta account, Sandbox, ngrok, a real phone or a speech-to-text service:

```
 virtual phone ──signed webhook──▶  screening app  (/api/channels/whatsapp|voice/…, /…/whatsapp/meta)
      ▲        ◀── TwiML (Twilio) ──     │
      │                                  │ real twilio SDK (TWILIO_API_BASE_URL)
      │                                  │ Graph API client (META_GRAPH_BASE_URL)
      │                                  │ STT client (STT_BASE_URL)
      └──── fake api.twilio.com ◀────────┤  Messages.create, media downloads
            fake graph.facebook.com ◀────┤  /messages, /{media-id}, read receipts
            fake /v1/audio/transcriptions┘  voice notes
      delivery receipts ──▶ app  (Twilio status callbacks / Meta `statuses` webhooks, asynchronous)
```

The app runs **unmodified**: it validates the signatures, answers TwiML or calls
the Graph API, downloads media, and transcribes voice notes through its real
clients. Only base URLs change (`TWILIO_API_BASE_URL`, `META_GRAPH_BASE_URL`,
`STT_BASE_URL`).

## Modules

One plugin per vendor, named like the backend's `integrations/<vendor>/`:

| Module | What it fakes |
|---|---|
| `core.py` | `ChannelSimulator` (the hub), `Phone`, `BaseCall`, message/call records, `Faults`, provider lists |
| `twilio.py` | Twilio: WhatsApp webhooks + REST API (`Messages.json`, media, status callbacks), voice calls (`TwilioCall`) |
| `twiml.py` | The TwiML interpreter used by `TwilioCall` |
| `meta.py` | Meta WhatsApp Cloud API: webhooks, Graph API, handshake |
| `vonage.py` | Vonage Voice: answer/event webhooks, NCCO, signed JWTs (`VonageCall`) |
| `speech.py` | Speech: how phone STT writes what callers say, simulated voice-note audio, the `/v1/audio/transcriptions` service |
| `server.py`, `static/` | The HTTP server: UI, control API, the fake vendor endpoints |
| `runner.py`, `testing.py` | Start the simulator + app as processes (`SimulatorServer`, `AppProcess`), in-process and live test stacks |

## Manual testing

```bash
python -m simulators --with-app --mock-llm                          # WhatsApp through Twilio
python -m simulators --with-app --mock-llm --whatsapp-provider meta # WhatsApp through Meta's Cloud API
python -m simulators --with-app --mock-llm --voice-provider vonage  # the phone line through Vonage (NCCO)
#   Channel simulator  http://127.0.0.1:8900/          phones: WhatsApp + calls
#   Screening app      http://127.0.0.1:8000/backoffice
```

`--with-app` starts the app wired to the simulator, with its own database
(`data/simulator.db`, `--db` to change it). In Meta mode it also runs the webhook
subscription handshake. Drop `--mock-llm` to use the model configured in `.env`.
Other options: `--whatsapp-profile construction_laborer`, `--voice-profile …`,
`--port`, `--app-port`, `--no-sign`; see `--help`. To use an app you start
yourself, run `python -m simulators` (without `--with-app`) and copy the
variables it prints.

The UI (`http://127.0.0.1:8900/`):

| Area | What you can do |
|---|---|
| **Teléfonos** | Add virtual phones (`Aleatorio` makes up a number). One number = one candidate, who can use both channels |
| **WhatsApp** | Chat like on a phone. **🎤** sends what you typed as a *voice note* (a playable simulated recording; pick its language), **📷** a photo (the text becomes its caption), **⏺** records a real voice note with the microphone (only a real STT can understand it). Bubbles show how each message arrived: `TWIML`, `API REST`, `API GRAPH`, `PLANTILLA`, *no entregado (error …)*, plus ✓✓ / ✓✓ leído. *Cerrar ventana 24 h* pretends the candidate's last message was a day ago |
| **Llamada** | *📞 Llamar*, then type what you would say and press *Hablar*. *Silencio* = the `<Gather>` times out, *Colgar* = the caller hangs up. The status line shows the STT locale Twilio listens in. Optional: *🔊 Leer en voz alta* (browser TTS) and 🎤 dictation (Chrome) |
| **Fallos del proveedor** | API outage, 24h window on/off, speech-to-text outage; Twilio: template-only sender, window errors sync/async. In Meta mode: *Verificar webhook* |
| **Tráfico** | Every webhook (→ app), status callback, REST/Graph/STT call (←) with parameters, answer, status, latency and error code |

A typical session: write on WhatsApp, answer a question with a voice note, send
a photo, call from the same number halfway, hang up, then finish on WhatsApp.
Watch the candidate in the backoffice (link in the header): one candidate,
*WhatsApp → Teléfono → WhatsApp*, every line labelled with how it was said.

## In tests

### In-process (default `pytest` suite, no sockets)

The `channel_stack` fixture (`tests/conftest.py`, used by `tests/integration/channels/`) boots the real app with FastAPI's
TestClient (MockLLM, temporary SQLite) and plugs the simulator into every side.
Webhooks go through the app's TestClient. The app's Twilio SDK talks to the
simulator directly. Graph API, media downloads and STT go to the simulator's
own HTTP app (a second TestClient). Signature validation stays on. The
`wa_provider` fixture runs a test once per WhatsApp provider:

```python
def test_whatsapp_to_voice(channel_stack, wa_provider):               # runs on twilio and meta
    stack = channel_stack(provider=wa_provider,
                          whatsapp_profile="construction_laborer",   # any settings override
                          voice_profile="construction_laborer")      # same process on both lines
    carlos = stack.sim.phone("+34600333444", "Carlos")

    carlos.whatsapp("hola")                      # -> ["¡Hola! Soy Andrés, de Constructora Horizonte…"]
    carlos.voice_note("sí, empecemos")           # transcribed by the app through the simulator's STT
    call = carlos.call()                         # runs the TwiML until Twilio listens
    assert "de nuevo" in call.prompt.lower()     # what the caller just heard
    call.say("Carlos Martín Ruiz")               # SpeechResult to the <Gather> action
    call.silence(); call.hangup()

    session = stack.session_for(carlos)          # public session API: status, data, messages, events
    assert conversation_as_stored(session) == redacted(conversation_as_lived(carlos))   # nothing lost
    stack.silence(session["id"], seconds=60)     # fake idle time + one re-engagement tick
    assert carlos.wait_for_whatsapp("sigues").pushed   # pushes arrive asynchronously
```

Helpers on the stack: `sessions_for(phone)`, `session(id)`, `candidate(id)`
(backoffice view), `wait_for_status(id)`, `silence(id, seconds)`,
`age(id, hours)`, plus `stack.api` (HTTP client on the app, recruiter
`X-Recruiter: Ana`), `stack.app` (the `backend.app` module) and
`stack.sim.flush()` (wait for pending delivery receipts).
`simulators.testing.conversation_as_lived(phone)` /
`conversation_as_stored(session)` / `redacted(lines)` compare what the
candidate experienced with what the backend recorded.

### The provider matrix

`wa_provider` / `voice_provider` (in-process) and `live` / `live_voice` (e2e)
take their values from the backend's provider registry
(`backend/integrations`). A provider registered there, with its simulator
plugin, is exercised by every contract test automatically; see
`docs/channel_architecture.md`. Channel-switch stories run across WhatsApp ×
voice combinations, and `tests/e2e/channels/test_journeys.py::test_a_stack_without_twilio_at_all`
runs a complete candidate on Meta + Vonage.

### Live (browser e2e)

`python -m pytest -m channels` runs the channel e2e suite: the app as a
uvicorn subprocess and the simulator's HTTP server, over real HTTP, with real
background workers and real time, plus a recruiter in Chromium. 73 tests,
most run once per WhatsApp provider:

| File | What it covers |
|---|---|
| `tests/e2e/channels/test_journeys.py` | Whole candidate journeys, each checked line for line (`assert_nothing_lost`) and in the backoffice. WhatsApp screening with a registry answer pushed at the end (pipeline + quick view). Phone-only screening with a plain STT engine (lowercase, ID digit by digit). WhatsApp → phone → WhatsApp with voice notes, a photo and silence. English across channels. One process per line. A rejected candidate who calls again. A silent caller closed and resumed on WhatsApp. A referral with a spaced phone number who calls. The *Canal* filter |
| `tests/e2e/channels/test_recruiter.py` | Recruiter ↔ candidate, two-sided. The transcript follows the candidate live (voice notes, calls). Quick reply from the pipeline. A call during a takeover leaves a message. A message to a phone-only candidate waits for WhatsApp. A manual ID check decided while the candidate waits on WhatsApp. Reopening a rejected candidate. A registry failure resolved by hand. Search by a phone number typed with spaces, CSV |
| `tests/e2e/channels/test_resilience.py` | Restarts and crashes mid-conversation and mid-call. A provider redelivery after a crash. Calls and messages during downtime. A provider outage queueing recruiter messages. A message outside the 24h window going out as a template (timeline). Meta dev-mode allow-list. STT outage. Slow turns answered through the API. Tunnels (`PUBLIC_BASE_URL`) and a wrong `PUBLIC_BASE_URL` (Twilio 403, Meta unaffected). Real-time nudges and closing. Stale nudges not replayed. 12 candidates on both channels at once. The same candidate on WhatsApp and on the phone at the same moment |
| `tests/e2e/channels/test_simulator_ui.py` | The simulator UI itself: WhatsApp and calls, voice notes, fault toggles, failed deliveries + templates, Meta webhook verification, rejected webhooks in the traffic log, silence / hang-up / call back |
| `tests/e2e/channels/test_smoke.py` | Smoke tests of the first iteration |

Fixtures (`tests/e2e/channels/conftest.py`, helpers in `tests/support/channels.py` and `tests/support/candidates.py`):

- `stacks.get(provider, sim_kwargs=None, **env)`: a running stack for that
  configuration, started on first use and shared for the session, with faults
  and the STT model reset. Different `env` gives a different stack: e.g.
  `WHATSAPP_SYNC_TIMEOUT_SECONDS="0"` for slow turns, a fast re-engagement
  clock, `STACK="restarts"` for tests that restart the app.
- `live`: the construction stack, once per provider (`[twilio]` / `[meta]`).
  `channels` / `channels_meta` / `sim`: the same for one provider.
- `construction_answers(name, document)`, `only_session(stack, phone)`,
  `assert_nothing_lost(stack, phone)`, `open_candidate(page, stack, id, tab)`,
  `ensure_profile(stack, id, source, mutate)` (a process variant, e.g. a
  manual ID check).
- `stack.restart_app(crash=False)` restarts the app on the same port and
  database (a deploy, or `kill -9` with `crash=True`). `stack.app_process.kill()` /
  `.start()` simulate downtime.

`simulators.testing.LiveStack(..., whatsapp_provider="meta")` is the same
harness for your own scripts.

### Python API

| Object | Members |
|---|---|
| `ChannelSimulator(app_url, transport=None, *, whatsapp_provider="twilio"\|"meta", voice_provider="twilio"\|"vonage", graph_url, whatsapp_number, voice_number, public_url, paths, sign, send_status_callbacks, transcriber, stt_api_key, twilio={...}, meta={...}, vonage={...})` (`simulators/core.py`) | `phone(number, name)`, `faults`, `media`, `log`, `flush()`, `snapshot()`, `reset()`; the plugins `twilio`, `meta`, `vonage`, `speech`, and `messaging` / `voice` (the plugin each channel runs on) |
| `sim.twilio` (`TwilioSim`, `simulators/twilio.py`) | `account_sid`, `auth_token`, `templates` (Content SID → body), `rest_messages` (every `Messages.create`), `rest_client()` (a real `twilio` SDK client wired to the simulator) |
| `sim.meta` (`MetaSim`, `simulators/meta.py`) | `templates` (by name), `sent` (every Graph API message), `verify_webhook()`, `access_token`, `app_secret`, `verify_token`, `phone_number_id` |
| `Phone` | `whatsapp(body)` → replies the candidate got while the app handled it; `voice_note(text, language)`, `photo(caption)`, `audio(bytes, mime)`, `resend_last()` (provider retry, same message id); `thread`, `received()`, `texts()`, `wait_for_whatsapp(match, since, timeout)`, `window_open()`, `close_window()`, `call()` → a call (`TwilioCall` / `VonageCall`, both `BaseCall`), `active_call` |
| `WhatsAppMessage` | `body`, `via` (`twiml`, `rest`, `graph`, `template`), `pushed`, `in_reply`, `delivered`, `status`, `error_code`, `read`, `transcript`, `media_id` |
| `BaseCall` (`simulators/core.py`) | `say(text, confidence)`, `silence()`, `hangup()`; `prompt`, `prompt_language` (TTS locale), `gather_language` (STT locale), `waiting`, `state`, `end_reason` (`hangup-by-app`, `caller-hangup`, `end-of-twiml`, `application-error`), `transcript`, `agent_lines()` |
| `TwilioCall` (`simulators/twilio.py`), `VonageCall` (`simulators/vonage.py`) | The `BaseCall` API; the Twilio one executes TwiML (`simulators/twiml.py`). For Vonage, `sid` is Vonage's call `uuid`. `sim.vonage.signature_secret` signs the webhooks |
| `Faults` | `rest_down`, `template_only`, `enforce_window`, `window_errors` (`"sync"`/`"async"`, Twilio), `webhook_timeout`, `stt_down`, `recipient_allow_list` (Meta dev mode, 131030), `receipts_before_response` (delivery receipts reach the app before the API call that sent the message returns) |

**Speech recognition output** (`simulators/speech.py`): by default calls post what
Twilio posts, a sentence (`twilio_style`: "Sí, tengo 34 años."). Other engines
can be modelled with `transcriber=speech.chain(speech.digit_by_digit,
speech.plain_lowercase)` ("mi dni es 1 2 3 4 5 6 7 8 z") or any function.
Voice notes are real WAV files whose "spoken" text travels in a RIFF INFO chunk
(`simulators/speech.py`). The simulator's `/v1/audio/transcriptions` reads it
back, and returns an empty transcript for real recordings.

### HTTP control API

Everything in the UI is also available over HTTP (OpenAPI at `/sim/docs`), so
any tool can script it:

```bash
curl -XPOST localhost:8900/sim/api/phones -H 'content-type: application/json' -d '{"number":"+34600111222"}'
curl -XPOST localhost:8900/sim/api/phones/+34600111222/whatsapp -H 'content-type: application/json' -d '{"body":"hola"}'
curl -XPOST localhost:8900/sim/api/phones/+34600111222/voice-note -H 'content-type: application/json' -d '{"text":"sí, tengo carnet"}'
curl -XPOST localhost:8900/sim/api/phones/+34600111222/photo -H 'content-type: application/json' -d '{"caption":""}'
curl -XPOST localhost:8900/sim/api/phones/+34600111222/calls                      # -> {"sid": "CA…", "prompt": …}
curl -XPOST localhost:8900/sim/api/calls/CA…/speech -H 'content-type: application/json' -d '{"text":"sí"}'
curl -XPOST localhost:8900/sim/api/faults -H 'content-type: application/json' -d '{"rest_down":true}'
curl -XPOST localhost:8900/sim/api/meta/verify                                     # webhook handshake
curl localhost:8900/sim/api/state                                                  # phones, threads, calls, log
```

## Fidelity

| Area | Simulated |
|---|---|
| Twilio WhatsApp webhook | Form fields Twilio sends (`MessageSid`, `AccountSid`, `From/To` with `whatsapp:`, `Body`, `NumMedia`/`MediaUrlN`/`MediaContentTypeN`, `ProfileName`, `WaId`…), `X-Twilio-Signature` over the exact URL, 15s timeout (11205), non-2xx → 11200 and no reply, invalid TwiML discarded |
| Meta webhook | `whatsapp_business_account` envelope with `metadata.phone_number_id`, `contacts`, `messages` (text, audio with `voice: true`, image with caption, button), `statuses` (sent/delivered/read/failed + `errors`), `X-Hub-Signature-256`, the `hub.*` handshake |
| Provider plugins | Messaging: `twilio`, `meta`. Voice: `twilio` (executes TwiML), `vonage` (executes NCCO). The caller-side API (`BaseCall.say/silence/hangup/prompt/gather_language…`) is the same for every voice provider (`BaseCall`), so tests never depend on the provider. Which plugin runs: `ChannelSimulator(whatsapp_provider=…, voice_provider=…)`, `--whatsapp-provider` / `--voice-provider` |
| Vonage Voice | Answer `GET` with `from`/`to`/`uuid` (numbers without "+"), NCCO `talk` + `input` (`type: ["speech"]`, `eventUrl`), input results as JSON (`speech.results[].text/confidence`, silence = `timeout_reason: "start_timeout"`), end of NCCO = hang-up, `answered` / `completed` events, every webhook signed with a JWT (HS256, signature secret, `payload_hash`) |
| Voice webhooks (Twilio) | `CallSid`, `CallStatus` (`ringing` → `in-progress` → `completed`), `SpeechResult` + `Confidence` posted to the `<Gather action>`, relative URLs resolved against the current document, **silence falls through to the next verb** (`<Redirect>`), end of document hangs up, `<Hangup>`, status callback with `CallDuration`, app errors read out ("an application error has occurred") and hang up, redirect loops detected |
| Twilio REST | `POST …/Messages.json` (free-form or `ContentSid` + `ContentVariables`, `StatusCallback` → signed `sent`/`delivered`/`read` or `undelivered` + `ErrorCode`), `GET …/Messages/{sid}.json`, `GET /Accounts/{sid}.json` (for `scripts/check_twilio.py`), media URLs with Basic auth; 20003 · 63007 · 21910 · 21619 · 21654 · 63016 · 20503 |
| Meta Graph API | `POST /{version}/{phone-number-id}/messages` (`text`, `template` with body parameters, `status: read`), `GET /{version}/{media-id}` + bearer-token download; error envelope with `type`/`code`/`fbtrace_id`: 190 bad token · 100 bad parameter / unknown object · 131030 recipient not allowed · 132001 unknown template · 2 outage; outside the 24h window: **accepted, then 131047** in a status webhook |
| Speech-to-text | OpenAI-compatible `/v1/audio/transcriptions` (multipart, `verbose_json`/`json`/`text`), bearer key, outage (503) |

Ordering: with Meta, replies are API calls, so the simulator sees them in the
order the app sends them. With Twilio, a TwiML reply is recorded when the phone's
thread has read the webhook response; under heavy concurrency an API message the
app sent right after that response may be recorded first. The app's ordering
guarantee is pinned by `tests/integration/channels/test_robustness.py`. The concurrency e2e
test compares transcripts in any order.

Not simulated: real audio understanding (phone speech and simulated voice notes
are text underneath), barge-in, DTMF input, interactive list messages sent by
the business, Sandbox `join` codes and expiry, rate limits and provider webhook
retry schedules (`resend_last()` replays one).

About 63016 on Twilio: the simulator can *refuse* free-form outside the window
(`window_errors="sync"`) or *accept it and fail it later*
(`"async"`), which we believe is what Twilio usually does. The app
handles both. Sync failures fall back to the template at once. Async ones come
back in the status callback, which needs `PUBLIC_BASE_URL`, and then trigger the
template. Meta always reports 131047 asynchronously.

## What the simulator found (and the status)

| Finding | Status |
|---|---|
| A candidate who switched channel (WhatsApp ↔ phone, or a recruiter referral who wrote on WhatsApp) got a **second screening** from scratch | **Fixed**: channel continuity (`docs/channels.md`) |
| After a phone → WhatsApp switch, the first WhatsApp message was taken as "hello" even when it **was an answer** ("Vivo en Getafe"): stored, never processed, the question asked again | **Fixed**: only greetings / "se me cortó la llamada" are treated as hello; anything else is a turn |
| The transcript did not record **which channel** each line came through, and missed lines the candidate heard on calls (silence re-prompts, the takeover notice) | **Fixed**: conversation registry (`channel`, `kind`, `meta_json`), checked line for line against the simulator |
| The transcript stored the agent's greeting **before** the candidate's message that triggered it | **Fixed** |
| A resumed WhatsApp conversation answered "perdona, ya estoy aquí" with "No lo he captado…" and **burnt a re-ask attempt** | **Fixed** |
| WhatsApp **voice notes** (and photos) got **no reply at all** and were not stored | **Fixed**: voice notes transcribed and answered; other media declined politely, never downloaded |
| WhatsApp messages that could not be pushed were **never delivered** | **Fixed**: delivered with the next reply |
| A push **accepted and failed later** (63016 / 131047) counted as delivered: no template, message lost | **Fixed**: delivery registry + status callbacks → template fallback + re-queue |
| A recruiter message replaced by the template counted as delivered | **Fixed**: it stays queued until the candidate answers |
| After in-call silence the caller heard "un reclutador revisa tu caso" | **Fixed** |
| **"Perdona, no te he oído" at "¿Eres mayor de 18 años?" rejected the candidate** (legal knockout), on calls and after a switch to WhatsApp ("no sé si me oyes", "no podía hablar") | **Fixed**: talk about the conversation is never a yes/no answer. A confirmation before knockouts is recommended (`docs/channels.md`) |
| On the phone, **"mi DNI es 1 2 3 4 5 6 7 8 z" was not understood**: re-asked until later answers landed on the wrong questions, ending in a wrongful "out of area" | **Fixed**: IDs said digit by digit, in pairs, with letters by name |
| Speech results ("Carlos Martín Ruiz.") were **rejected as names** | **Fixed**: final punctuation and casing tidied |
| A call during a recruiter takeover asked the pending question, then said the recruiter would answer "por aquí"; silence would close the screening | **Fixed**: the call takes a message, silence ends it politely |
| A check decision could **arrive before the reply** to the candidate's last answer ("cumples los requisitos", then "estoy verificando") | **Fixed**: pushes wait for the candidate's turn in flight |
| A failure receipt arriving before the send call returned was lost: **the recruiter's message was marked delivered** although it never arrived (flaky) | **Fixed**: outbox (store, push, settle) + early receipts replayed |
| After a restart, the **end of a call** was not recorded, and a **provider redelivery** after a crash was answered twice | **Fixed**: call and inbound ids persisted |
| A phone candidate who later wrote on WhatsApp first got the queued **"¿Sigues ahí?" nudges** | **Fixed**: undelivered nudges are superseded when the candidate is back |
| On the phone, an ID spelled out in *words* ("doce millones…") is re-asked with "Escríbelo así" | Open, minor (speech recognition writes digits) |
| A phone candidate whose decision arrives after they hang up, and who then writes on WhatsApp, gets a **new screening**, not the decision | Open (product decision) |
