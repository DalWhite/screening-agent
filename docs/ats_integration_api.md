# ATS integration API — Grupo Sazón screening agent

The screening service is a plain REST API. An ATS (Greenhouse, Lever, Ashby, or Grupo Sazón's internal tool) integrates by driving sessions and pulling structured results — no scraping, no screen-reading.

## Session lifecycle from an ATS perspective

```
ATS webhook (new application)
  → POST /api/sessions                     create session, get opening message
  → deliver reply via candidate channel (email/SMS/WhatsApp)
  → POST /api/sessions/{id}/messages       relay each candidate reply
  → GET  /api/sessions/{id}/pending        poll for proactive messages (re-engagement nudges)
  → on status != "active": read summary → create/advance ATS stage
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + model availability + active session count |
| `POST` | `/api/sessions` | Create session. Body: `{"source": "web|whatsapp|sms|phone|ats", "language": "es|en"}` (language optional — auto-detected). Returns `session_id`, opening `reply`, `stage`, `language` |
| `POST` | `/api/sessions/{id}/messages` | Send candidate message. Body: `{"text": "..."}`. Returns `reply`, `stage`, `status`, `language`, `advanced`, `validation_error`, `guardrail`, `rag_used`, and `summary` when the session closes |
| `GET` | `/api/sessions/{id}/pending` | Undelivered proactive messages (re-engagement nudges). Client polls ~every 15s |
| `POST` | `/api/sessions/{id}/end` | Candidate-initiated early exit; closes with qualified/abandoned outcome |
| `GET` | `/api/sessions/{id}` | Full session: metadata, structured `data`, `summary`, full `messages` transcript, `events` audit log |
| `GET` | `/api/sessions?status=&qualified=&q=&limit=` | Recruiter list: filter by status (`active/qualified/disqualified/abandoned`), qualified flag, free-text `q` (name/city) |
| `GET` | `/api/analytics` | Funnel, completion/qualified rates, drop-off by stage, disqualify reasons, language mix, re-engagement recovery, top FAQ hits |

## The recruiter payload (what an ATS should store)

`summary` is the contract — stable, versioned JSON produced for every closed session:

```json
{
  "qualified": true,
  "outcome": "qualified | disqualified | abandoned",
  "disqualify_reason": "no_license | out_of_area | silence | null",
  "completed_stages": ["intro", "name", "license", "location", "availability", "schedule", "experience", "start_date", "review"],
  "dropped_at_stage": null,
  "duration_seconds": 214,
  "candidate_turns": 9,
  "languages": ["es", "mixed"],
  "data": {
    "full_name": "Laura Jiménez Ortiz",
    "has_license": true,
    "city": "Madrid", "zone": "Malasaña", "country": "ES", "in_service_area": true,
    "availability": ["full_time"],
    "preferred_schedule": "morning",
    "experience_years": 2, "experience_platforms": ["glovo"],
    "start_date": "2026-11-02"
  },
  "recruiter_notes": ["Laura Jiménez Ortiz — Madrid (Malasaña), ES — jornada completa — turno mañana — 2 años de experiencia (glovo) — disponible desde 2026-11-02 — licencia de conducir vigente"],
  "flags": ["unresolved:availability", "needs_review", "waitlist_out_of_area", "abusive_language"],
  "generated_at": "2026-10-12T12:42:39Z"
}
```

Mapping suggestion: `qualified:true` → ATS stage "Screening passed"; `flags` containing `needs_review` → stage "Needs human review"; `disqualified` → archive with reason tag; `abandoned` → trigger ATS-side re-contact campaign.

## Webhook out (recommended addition)

For push-based integration, add a `WEBHOOK_URL` setting that POSTs the summary JSON on session close — one outbound call per completed screening, with `X-Signature` HMAC. The polling design works today; the webhook is the production polish.

## Auth & multi-tenancy notes

- Pilot: single-tenant, dashboard behind SSO, API behind an API key header.
- The `source` field already segments channels for per-channel analytics; an `org_id` field is the natural extension for multi-client use.
