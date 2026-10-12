# Grupo Sazón — AI Candidate Screening Agent

An AI screening agent for **Grupo Sazón** (45 restaurant locations, Spain & Mexico) that pre-screens delivery-courier candidates in a natural Spanish/English conversation, extracts structured data, validates it against hard hiring rules, and hands recruiters a clean, ranked pipeline with analytics.

Built for the Orbio FDE technical assignment. See `docs/process_design.md` (process design) and `docs/architecture.md` (technical design).

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy .env.example .env                              # point LLM_BASE_URL/LLM_API_KEY at your model server
python -m uvicorn backend.app:app --port 8000
```

Open:

| URL | What |
|---|---|
| `http://127.0.0.1:8000/` | Candidate chat (try it in Spanish or English) |
| `http://127.0.0.1:8000/dashboard` | Recruiter dashboard: live sessions, transcripts, structured JSON, analytics |
| `http://127.0.0.1:8000/voice` | Voice front-end for the same agent (Web Speech API) |

No model server reachable? Set `LLM_FALLBACK=mock` — the agent runs fully on deterministic templates (demo safety net). Run a second instance for demos without touching your real data:

```bash
LLM_FALLBACK=mock DB_PATH=data/sazon_test.db python -m uvicorn backend.app:app --port 8001
```

## Demo script (5 minutes)

1. Chat page: screen a qualified candidate in Spanish (name, licence yes, "Madrid, Malasaña", full-time, morning, "2 años en Glovo", start date). Ask **"¿Cuánto pagan por hora?"** mid-conversation — the agent answers from the FAQ (RAG) and re-asks the pending question.
2. Switch languages mid-chat (English answer to a Spanish question) — the agent follows the candidate.
3. Chat page: "no tengo licencia" → soft disqualification + bike-waitlist offer.
4. Chat page: type nothing for ~45s → re-engagement nudge arrives automatically.
5. Dashboard: see both sessions, transcripts, extracted JSON, flags, funnel and drop-off analytics.

> Tip: run the demo server standalone (not while another app is hammering the same local model server) — the local inference slot is single-threaded.

## How it works

**Hybrid deterministic + LLM.** A stage machine (`intro → name → licence → location → availability → schedule → experience → start date → review → outcome`) owns the flow, validation, branching and disqualification — auditable, never hallucinated. The LLM does exactly two jobs:

1. **Extraction** — one tool call per candidate message returns structured JSON (intent, language, sentiment, and any screening fields mentioned).
2. **Phrasing** — the agent decides *what* to say; the LLM words it in the candidate's language and register.

If the model is unreachable, every message falls back to hand-written bilingual templates, so the product never crashes. Full details and trade-offs: `docs/architecture.md`.

Key properties:

- **Validation, not vibes** — licence is hard-required; cities are matched against the 45-city service-area file (aliases, fuzzy match, ambiguity handling); dates accept ISO and natural language ("mañana", "el lunes", "en 2 semanas"); 3 failed attempts accept-with-flag instead of looping forever.
- **Bilingual + code-switching** — per-message language detection with conversation-level dominance; templates and FAQ are ES/EN.
- **Guardrails & privacy** — prompt-injection and abusive-input screening, no document/bank requests, PII redaction from stored notes, 90-day retention, GDPR/Mexico data-residency rationale for running a local model.
- **RAG** — BM25 over a 26-item bilingual FAQ; answers only from retrieved context, with the FAQ id logged per turn.
- **Re-engagement** — background monitor nudges silent candidates at 45s/120s, closes as `abandoned(silence)` at 300s; recovery rate is tracked in analytics.
- **Storage** — SQLite (sessions/messages/events), full conversation history, structured summary per session, REST API for a future ATS integration.

## Tests & evals

```bash
python -m pytest tests/ -q        # 52 unit/scenario tests
python tests/evals/run_evals.py   # 6 end-to-end conversation evals -> tests/evals/report.json
```

Evals cover: qualified ES + FAQ question, qualified EN code-switching, disqualified (no licence), disqualified (out of area + waitlist), give-up → `needs_review` flags, abusive input → abandoned.

## Sample conversations

`samples/` contains five full transcripts with stage transitions, RAG hits and the final recruiter summary JSON: qualified (ES, with pay question), qualified (EN code-switch), disqualified (no licence), disqualified (out of area), abandoned (needs review). Regenerate with `python scripts/generate_samples.py`.

## Key decisions & trade-offs

| Decision | Why | Trade-off accepted |
|---|---|---|
| Local quantized model (Qwen3.8-Flash-Next IQ3, llama.cpp) | ~1k–10k conversations/week at near-zero marginal cost; candidate data stays in-region (Spain/Mexico); no vendor lock-in | Lower raw model quality — compensated by deterministic validation, tool-call-only extraction and template fallback |
| Deterministic stage machine, LLM only extracts/phrases | Disqualification decisions must be auditable; hallucinated hiring decisions are unacceptable | Less "free-form" conversation; flow is guided |
| BM25 instead of embeddings for FAQ | 26 docs: faster, deterministic, inspectable, zero dependencies | Won't handle heavy paraphrase — swap-in point documented |
| SQLite | Single-file, zero-ops, right size for one client pilot | Not multi-node — Postgres migration path documented |
| Accept-with-flag after 3 failed re-asks | Never trap a real candidate in a validation loop | Recruiter sees `unresolved:<stage>` + `needs_review` flags instead of clean data |

## Improvement ideas (next iteration)

- Phone/SMS/WhatsApp channel — 60% of candidates don't answer calls; the agent should reach them where they are.
- Embedding-based retrieval + query rewriting once the FAQ grows past a few hundred items.
- ATS write-back (Greenhouse/Lever APIs) and a recruiter ranking score trained on interview outcomes.
- Voice with real telephony (Twilio + streaming STT/TTS) instead of browser Web Speech.
- Continuous evals in CI against a live model, plus drift monitoring on extraction accuracy.
- Multilingual beyond ES/EN (Catalan, Galician, Portuguese) for Spain/LatAm coverage.

## Project layout

```
backend/
  app.py               FastAPI app + REST API
  config.py            .env-driven settings
  reengagement.py      silence monitor (nudges / abandonment)
  llm/client.py        OpenAI-compatible client, tool-call extraction, MockLLM fallback
  screening/           agent.py (stage machine), schema.py, prompts.py, validation.py,
                       language.py, guardrails.py, service_areas.py, rag.py
  storage/db.py        SQLite persistence
  analytics/metrics.py funnel / drop-off / recovery metrics
  data/                service_areas.json (45 cities), faq.json (26 items)
frontend/              chat, dashboard, voice (vanilla JS)
tests/                 pytest suite + evals/
scripts/               generate_samples.py
docs/                  process_design.md, architecture.md
samples/               generated transcripts
```
