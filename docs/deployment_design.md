# Deployment design — Grupo Sazón screening agent

From pilot (this repo) to production for ~1,000 applications/week (~10k–15k conversations/week across channels).

## Topology

```
Candidate channels (web / WhatsApp / SMS / phone)
        │
   API gateway (FastAPI, 2+ replicas behind nginx)
        │                         │
   Screening service        Re-engagement monitor (single leader, Redis lock)
        │
 ┌──────┴───────┐
 │  LLM layer   │  llama.cpp server, 2 replicas (one per region)
 │  local model │  qwen3.x quantized, GPU or 64GB CPU host
 └──────────────┘
        │
 Postgres (sessions/messages/events)  +  nightly purge job (90-day retention)
```

## Scaling math

- 15k conversations/week ≈ 25 conversations/hour business-hours peak ≈ 1 message/minute. A single quantized model replica handles this with headroom; run two for availability and regional residency.
- Extraction calls are ~600–900 prompt tokens + a tool call; phrasing calls ~400 tokens. At 100 tok/s that is <10s worst case per turn — acceptable for chat, not for voice (voice needs a smaller/faster model or speculative decoding).
- SQLite → Postgres is the first code change: `storage/db.py` is the only module that touches SQL.

## Regions & data residency

- **Spain candidates** → model + DB in EU region (GDPR/LOPD). **Mexico candidates** → model + DB in Mexico region (LFPDPPP). Route by candidate country at session creation.
- Local model is the default precisely for this: candidate PII never leaves the region, and per-conversation cost is ~0.

## Availability & fallback ladder

1. Primary model replica.
2. Secondary replica / region.
3. Smaller local model (distilled) if the primary is down.
4. `LLM_FALLBACK=templates`: deterministic template replies — flow, validation and disqualification keep working; only phrasing degrades.
5. Human handoff: "un reclutador te llamará" — the agent always knows how to lose gracefully.

Health endpoint reports model availability; an external uptime probe polls it and pages on `llm_available:false` for >5 min.

## Production settings (vs demo defaults in `.env.example`)

| Setting | Demo | Production |
|---|---|---|
| `REENGAGE_FIRST_DELAY_SECONDS` | 45 | 180–300 (don't nudge too fast) |
| `REENGAGE_SECOND_DELAY_SECONDS` | 120 | 3600 + next-day SMS follow-up |
| `SESSION_IDLE_SECONDS` | 300 | 86400 (sessions resumable for 24h) |
| `LLM_TIMEOUT` | 45 | 15 (fail fast to fallback) |
| `RETENTION_DAYS` | 90 | 90 (legal ceiling; purge job nightly) |
| DB | SQLite | Postgres, PITR backups |

## Observability

- Per-turn structured log: stage, intent, validation result, RAG ids, fallback used, latency.
- Weekly eval run (`tests/evals/run_evals.py`) against the live model; alert if pass rate drops.
- Dashboard already tracks funnel/drop-off/recovery — production adds extraction-accuracy spot checks (recruiter thumbs-up/down on summaries feeds a fine-tuning dataset).

## Security

- No secrets in code; `.env` per environment; API keys rotated.
- Guardrails run before the LLM (injection/abuse screening) and after (PII redaction before storage).
- Rate-limit chat endpoints per IP/session; recruiter dashboard behind SSO + role-based access.
