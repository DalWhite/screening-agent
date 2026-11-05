# AI Candidate Screening Agent

A conversational screening agent that pre-screens candidates in natural Spanish/English (web chat, WhatsApp, phone), extracts structured data, applies each job's hiring rules, and hands recruiters a ranked pipeline in a **recruiter backoffice**.

One engine runs **many recruiting processes**. Each process is a declarative *screening profile*: its persona, questions, knockout and review rules, **external (asynchronous) checks**, scoring, FAQ and coverage areas. Recruiters manage processes in the backoffice with an editor that has a **basic mode** (questions and rules written as sentences) and an **advanced mode** (ids, data fields, vocabularies, external checks). It supports drafts, per-field validation, publication as versioned revisions, history and a simulator. Two profiles ship as seeds:

| Profile | Company | What it screens |
|---|---|---|
| `delivery_courier` | Grupo Sazón | Delivery drivers in 45 ES/MX cities: licence (knockout + manual, non-blocking licence check by a recruiter), area, availability, shift, experience, start date |
| `construction_laborer` | Constructora Horizonte | **Low-qualification construction labourers**: 18+ and right to work (legal knockouts), **ID document verified asynchronously against an identity registry** (blocking), site area, physical work (flag for review, not a rejection), PRL safety course (flag; the company trains), trades experience, transport, start date |

Docs: `docs/screening_profiles.md` (how to configure a process), `docs/external_checks.md` (asynchronous validations), `docs/backoffice.md` (recruiter portal), `docs/process_design.md`, `docs/architecture.md`, `docs/channels.md`, `docs/ats_integration_api.md`, `docs/load_testing.md` (concurrency & resource usage).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # point LLM_BASE_URL/LLM_API_KEY at your model server
python -m uvicorn backend.app:app --port 8000
```

| URL | What |
|---|---|
| `http://127.0.0.1:8000/` | Candidate chat: job picker, then text or voice screening |
| `http://127.0.0.1:8000/?job=construction_laborer` | Direct link to one process (what you publish in a job ad) |
| `http://127.0.0.1:8000/backoffice` | **Recruiter backoffice**: pipeline, candidates, screening results, analytics, processes |

No model server? Set `LLM_FALLBACK=mock` and the agent runs fully on deterministic parsing and templates. To run a throwaway demo instance:

```bash
LLM_FALLBACK=mock DB_PATH=data/demo.db python -m uvicorn backend.app:app --port 8001
```

### Demo data (about 1000 candidates)

To try analytics, the pipeline and search with realistic volume, fill a database with generated candidates and point the server at it:

```bash
python scripts/seed_demo_data.py --db data/demo.db --reset          # 1000 candidates over 90 days, ~1.5 min
LLM_FALLBACK=mock DB_PATH=data/demo.db python -m uvicorn backend.app:app --port 8001
# → http://127.0.0.1:8001/backoffice#/analytics
```

Options: `--count N`, `--days N`, `--seed N` (same seed, same mix), `--append` (add to an existing database). The script refuses to write into a database that already has candidates unless you pass `--reset` or `--append`.

Nothing is inserted straight into the tables. Every candidate is screened by the real engine (MockLLM) and every recruiter action goes through the backoffice service, on a **simulated clock**, so funnels, drop-off, durations, FAQ usage, external checks, nudges and the audit timeline are consistent, as if the system had been live for the whole period:

| What | Mix |
|---|---|
| Processes | ~60% construction labourer, ~40% courier (ES + MX cities) |
| Channels / languages | web, WhatsApp, voice, manual referrals; ~13% English |
| Outcomes | pre-qualified (with PRL / physical / transport alerts), knockouts (underage, no work permit, out of area, ID not found in the registry, no licence), incomplete (silent after two nudges, by question), some recovered after a nudge, a few still chatting |
| External checks | ID registry valid / not found / registry down (solved by hand), manual licence checks decided by recruiters (recent ones pending) |
| Recruiters | 4 recruiters triaging: owners, tags, notes, review decisions, shortlisted → interview (with date) → offer → hired, rejections at every step, human takeovers (a few still waiting for an answer) |

Volume is growing towards today, weekdays are busier than weekends, and recent candidates haven't been touched by recruiters yet. Conversations "still running" are placed a few minutes before the script ends; once the server runs, the re-engagement monitor nudges and closes them as it would in production.

Set `BACKOFFICE_PASSWORD` (user `BACKOFFICE_USER`, default `recruiter`) to protect the backoffice with HTTP Basic auth. Without it the portal is open, which is only acceptable for local development.

## Demo script (10 minutes)

1. **Construction chat** (`/?job=construction_laborer`): "Carlos Martín Ruiz" → "sí, tengo 34 años" → "sí" → "12345678Z" (ID: masked, verified in the background) → "Getafe" → "sí" → "no" (no PRL course) → "3 años de albañil" → "tengo coche" → "el lunes". The candidate is pre-qualified with a `needs_prl_training` flag once the registry confirms the ID. Ask "¿Dais las botas?" mid-way and the answer comes from the construction FAQ. Try "12345678A" (wrong letter, caught locally) or "99000001C" (unknown to the registry: the knockout arrives *while you keep chatting*).
2. **Knockouts**: "tengo 17 años" gives an immediate, respectful rejection (legal requirement). "no puedo cargar peso" does *not* reject: the candidate lands in "Por revisar".
3. **Courier chat** (`/?job=delivery_courier`): the original process is unchanged ("no tengo carnet" → disqualified + bike waitlist).
4. **Backoffice → Pipeline**: drag Carlos from *Pre-calificado* to *Preseleccionado*, or click his card to triage him in the **quick view** (answers, alerts, checks, quick reply, one-click *Avanzar a…*). Stages are editable in place: double-click a column title to rename it, or use its menu to recolour, reorder, add or delete stages. Open the full file to see **Perfil** (editable data, owner, tags, notes), **Resultados del screening** (answer per question with the candidate's literal words, rules evaluated, score breakdown) and **Actividad**.
5. **Manual interaction**: in a candidate's *Conversación* tab, write a message. The assistant pauses and the message appears in the candidate's chat (or WhatsApp). The candidate's reply comes to you; *Devolver al asistente* makes it pick up the pending question. *Reabrir screening* re-asks any question, even after a rejection.
6. **Candidatos**: search "albañil" (searches conversations too), filter by stage / outcome / alert chips, open *Más filtros* for score, dates, city, tags…, change a stage straight from the table, share the URL, bulk-move, export CSV. Press `/` anywhere for the global search.
7. **Analítica**: click any KPI or chart bar (e.g. a knockout reason, the question where people drop off) to open the matching candidates with the same filters.
8. **＋ Nuevo candidato**: register a referral by hand and send them the screening link.
9. **Procesos → Editar**: change a question's wording, add a rule as a sentence (*If Transporte a obra is Sin medio propio → discard*), try it in **Probar**, then **Publicar**. New candidates get revision 2; candidates mid-conversation finish on revision 1. Delete a question a rule depends on: the problem is pinned to the rule's cell and publishing is blocked.
10. **Verificaciones externas** (turn on *Configuración avanzada* in the editor): switch the ID check to *Manual (reclutador)* in a duplicated process, or look at a candidate whose registry check failed ("98000001Z"): retry it, or mark it ✓ Válido from the candidate file. The candidate's chat receives the final decision.

## How it works

**Hybrid deterministic + LLM, driven by profiles.** A generic stage machine runs the profile's question flow (`intro → questions… → review → outcome`) and owns validation, branching, knockouts and flags. It's auditable and never hallucinated. The LLM does two jobs only:

1. **Extraction**: one tool call per candidate message. The tool schema is **generated from the profile's questions**, so each process gets its own fields.
2. **Phrasing**: the agent decides *what* to say; the LLM words it in the candidate's language, in the profile's persona.

Questions are built from reusable **typed field handlers** (`name`, `yes_no`, `choice`, `multi_choice`, `number`, `experience`, `date`, `location`, `text`). Each handler has deterministic parsing, so if the model is unreachable (or for tests: `MockLLM`) every process still works end to end.

Key properties:

- **Business rules as data, edited by recruiters**: `disqualify` rules (field conditions like `work_permit eq false`, or validation errors like out-of-area after one retry) and `flag` rules (e.g. no safety course) that can route a candidate to human review. CRUD in the backoffice (basic / advanced editor), draft → publish → immutable revision, sessions pinned to their revision, history and restore, rule simulator.
- **External asynchronous checks**: ID registries, webhook services or manual recruiter checks. Results land in a data field that the same rules engine acts on (knockout mid-conversation, `verifying` outcome for blocking checks, review on failure). Outbox and worker with retries, deadlines and HMAC-signed callbacks. ID numbers are never sent to the LLM, are stored masked, and are purged after verification.
- **Ranking score** (0-100) per profile, explained criterion by criterion in the backoffice.
- **Validation, not vibes**: coverage areas with aliases, zones and fuzzy matching; ES/EN natural dates ("en 2 semanas", "next Monday"). After 3 failed re-asks the answer is accepted with a flag instead of looping forever.
- **Bilingual + code-switching** per message. A bare name or city keeps the conversation language.
- **Guardrails & privacy**: prompt-injection/abuse/self-harm screening; the assistant never asks for documents unless the process has an explicit ID question (then the number is parsed outside the LLM, stored masked and purged after verification); PII redaction (incl. DNI/NIE/passports); 90-day retention. Recruiter-only data (tags, notes, owner, score) never leaves through the candidate-facing API.
- **RAG**: BM25 over each profile's FAQ; answers only from retrieved context.
- **Re-engagement & resume**: silent candidates are nudged and then closed as abandoned; they can resume within 48h.
- **Human takeover**: recruiters can pause the assistant and talk to the candidate on the same channel. Nudges stop, and safety guardrails keep working.
- **Channels**: web, WhatsApp and voice (Twilio), each number bound to a process (`WHATSAPP_PROFILE`, `VOICE_PROFILE`).

## Tests

```bash
python -m pytest                        # 269 unit/API/scenario tests (tests/), no network, ~30s
python tests/evals/run_evals.py         # 12 scripted conversation evals, both processes -> tests/evals/report.json
python scripts/validate_profiles.py     # validate every screening profile (CI-friendly exit code)
```

### End-to-end (Playwright)

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest e2e                    # 54 browser tests against a live local server (~2-3 min)
python -m pytest e2e --headed --slowmo 300   # watch them run
```

The e2e suite starts its own server (MockLLM, throwaway database, random port, mock ID registry answering in 1s); there's nothing to set up. It covers:

| File | Objective |
|---|---|
| `e2e/test_candidate_chat.py` | Multi-process engine: job picker, construction screening (qualified after the async ID check, underage knockout, out-of-area retry + waitlist, FAQ, English), courier regression, reload |
| `e2e/test_backoffice_pipeline.py` | Pipeline management & manual interaction: board placement, drag & drop, card menu, **quick view triage**, **stages edited in place** (rename, recolour, add, reorder, delete moving candidates), busy columns scroll and link to the full list, interview scheduling, **two-browser takeover** (recruiter ↔ candidate live), reopening a rejected candidate, manual candidate + screening link |
| `e2e/test_backoffice_candidate.py` | Candidate profile (data, corrections with audit + re-score, owner/tags/notes) and screening results (answers, literal quotes, rules, score, knockouts, timeline) |
| `e2e/test_backoffice_search.py` | Advanced search: accent-insensitive, conversation search, filter chips + collapsible *Más filtros* + active-filter pills / *Limpiar*, shareable URLs, score range + sorting, saved views, inline stage change, global search, bulk actions, CSV export |
| `e2e/test_backoffice_analytics.py` | Analytics drill-down: KPI cards and chart rows open the candidate list with the same filters and the same counts |
| `e2e/test_backoffice_processes.py` | Process CRUD in the **basic / advanced editor**: edit a question and publish a revision (running candidates stay pinned), add a knockout rule and try it in the simulator, per-cell problems block publication / discard, scoring + settings, create a process from scratch and screen with it, duplicate/delete, deactivate, restore a revision |
| `e2e/test_external_checks.py` | Asynchronous checks: local control-letter validation, registry knockout arriving mid-chat, **blocking manual check decided by a recruiter while the candidate waits** (two browsers), registry failure → review → retry → manual resolution, non-blocking manual licence check, ID never shown in clear, recruiter correction re-verifies |

On failure, `--tracing retain-on-failure` keeps a Playwright trace in `test-results/`.

### Load testing: does it hold up under concurrency?

```bash
python -m pytest e2e/stress -m stress -s     # opt-in: not part of `pytest` or `pytest e2e` by default
```

`e2e/stress/` runs ~100 candidates (organic, varied scripts through plain HTTP — knockouts,
reviews, FAQ, bilingual, drop-offs) and ~10 recruiters (real headless browsers: board, search,
quick view, stage moves, notes, analytics, CSV export, manual candidates) against the same server,
database and background workers as production, and measures it from the outside: server CPU/RSS/
thread count over time, and latency/throughput/error-rate per endpoint and per recruiter action.
It asserts correctness (no crash, no lost or corrupted data, background workers keep working) is
unconditional, while responsiveness is reported, not gated — see `docs/load_testing.md` for what we
found (short version: correct and stable at 100 candidates / 10 recruiters on a 4-core machine, but
recruiter pages slow to several seconds and some requests queue past 15-30s — a single-process
thread-pool limit, not a SQLite or correctness problem) and what to change if that volume is a real
target.

## Adding a new recruiting process

In the backoffice: **Procesos → Nuevo proceso** (or *Duplicar*), write the questions and rules, try it in **Probar**, **Publicar**. No code or deployment is needed; the chat, channels, search, analytics and CSV export pick it up at once.

For processes that should live in the repository, add a seed folder `backend/data/profiles/<id>/` and check it with `python scripts/validate_profiles.py <id> --try "hola" …`. Seeds are imported as revision 1; later changes to the file are imported as new revisions. See `docs/screening_profiles.md`.

## Key decisions & trade-offs

| Decision | Why | Trade-off accepted |
|---|---|---|
| Declarative profiles + typed field handlers | New processes without code; rules reviewable by non-engineers; one engine to test | Exotic question types need a new handler (one class) |
| Flag rules with `review: true` instead of more knockouts | Low-qualification hiring must not silently reject on capability; humans decide | More candidates in "Por revisar" |
| Age asked as yes/no with a numeric fallback | Data minimisation: we only need "18+", yet "tengo 17" is still understood | — |
| Profiles in the DB as immutable revisions, edited as drafts | Recruiters change rules without deployments; drafts may be inconsistent while editing; every decision is traceable to the exact rules that made it | Seeds in git and the DB can diverge (file changes are re-imported as new revisions) |
| External checks write a data field that ordinary rules act on | One rules engine for answers and external validations; no special cases | Profile authors must add the rule that acts on the result |
| Outbox + background worker for checks | Candidate turns never wait on third parties; restarts lose nothing; retries and deadlines in one place | Results arrive up to one worker tick later |
| Recruiter pipeline separate from screening outcome | The agent decides the screening; recruiters own the hiring stages (manual moves stick) | Two status concepts, shown together in the UI |
| Human takeover pauses the agent per session | One conversation, one channel, no bot talking over the recruiter | The recruiter must hand back (or reopen) explicitly |
| Vanilla ES-module SPA for the backoffice | No build step, offline, same deployment as the API | Less tooling than a framework |
| Local quantized model, deterministic fallback | Near-zero marginal cost, data residency | Lower model quality, compensated by validation |
| SQLite (WAL) | Zero-ops pilot; readers never block candidate writes | Not multi-node; `storage/db.py` is the only SQL module |

## Project layout

```
backend/
  app.py               FastAPI app: chat API, profiles API, Twilio webhooks
  config.py            .env-driven settings
  pipeline.py          recruiter pipeline stages + automatic placement
  session_store.py     in-memory registry backed by SQLite
  reengagement.py      silence monitor (skips human takeovers)
  screening/           agent.py (generic engine), profile.py (profiles, rules, checks, scoring, registry),
                       fields.py (typed field handlers incl. id_document), prompts.py, validation.py,
                       language.py, guardrails.py, service_areas.py, rag.py
  backoffice/          router.py (/api/bo), service.py (recruiter actions), profiles_admin.py
                       (process CRUD, drafts, publish, simulator), report.py, labels.py, auth.py
  verifications/       service.py (outbox worker), providers.py (mock registry, webhook, manual)
  channels/            WhatsApp + voice (Twilio)
  llm/client.py        OpenAI-compatible client (profile-built tool), MockLLM
  storage/             db.py (SQLite persistence, migrations, search, editable pipeline stages), profile_store.py (revisions, drafts)
  analytics/metrics.py funnel / drop-off / pipeline metrics per process
  data/profiles/       one folder per recruiting process (profile.json, faq.json, service_areas.json)
frontend/
  index.html, static/chat.js              candidate chat (job picker, voice, recruiter messages)
  backoffice.html, static/bo/             recruiter backoffice (ES modules, no build step):
                                          backoffice.css (design system), dom.js + icons.js (UI kit),
                                          shared.js (stages, pills), views/*.js (one per screen)
tests/                 pytest suite + evals/
e2e/                   Playwright end-to-end suite; e2e/stress/ = opt-in load tests (docs/load_testing.md)
scripts/               seed_demo_data.py (demo volume), validate_profiles.py, generate_samples.py, check_twilio.py
docs/                  design + operations docs
samples/               generated transcripts (both processes)
```
