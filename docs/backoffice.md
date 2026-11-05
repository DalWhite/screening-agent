# Recruiter backoffice

`/backoffice` replaces the old read-only `/dashboard` (which now redirects to the analytics view). It's built around what a recruiter actually does in a day: triage new screenings, talk to candidates, move them through hiring stages, and find people again later.

Stack: vanilla ES modules (`frontend/static/bo/`), with no build step, served by the same FastAPI app and backed by the `/api/bo` REST API (`backend/backoffice/`). All views are URLs (`#/pipeline`, `#/candidates?...`, `#/candidates/<id>?tab=...`), so any view can be bookmarked or shared.

To try it with realistic volume (about 1000 candidates in every stage, 90 days of history), generate demo data with `python scripts/seed_demo_data.py --db data/demo.db --reset` and start the server with `DB_PATH=data/demo.db` (see the README).

## Design principles

- **One design system** (`backoffice.css` tokens, `dom.js` / `icons.js` kit): the same buttons, chips, stage pills, menus, drawers and dialogs on every screen. Light theme, one accent colour, one colour per pipeline stage, used consistently on the board, in the list, in the quick view and on the candidate file.
- **Fewest possible steps**: what a recruiter does most often takes one click from where they already are. Triage in a **quick view** drawer without leaving the board or the list; change the stage from the **stage pill** anywhere; **Avanzar a «next stage»** in one click; a **global search** (`/`) in the top bar; quick replies and quick notes inside the drawer.
- **Progressive disclosure**: rare options sit behind a *⋯* menu, *Más filtros* or *Configuración avanzada*, not on screen all the time.

## Screens

| Screen | What the recruiter does there |
|---|---|
| **Pipeline** | Kanban with one column per hiring stage. Drag & drop cards, or use the card's *⋯ → Mover a*. Clicking a card opens the **quick view**. Cards show process, city, score, alerts, owner and a **"Esperando tu respuesta"** badge when a candidate replied during a takeover. **Stages are editable in place**: double-click a title to rename; the column menu recolours, moves left/right, adds a stage after it, or deletes it (when it has candidates, a dialog asks where to move them). *Nueva etapa* at the end of the board. Each column scrolls on its own and shows the 50 most recent cards; *Ver los N en la lista* opens the full stage in *Candidatos* with the same search and owner filter. The four system stages (*En screening, Por revisar, Pre-calificado, Descartado*) are used by the automatic placement and can be renamed/recoloured but not deleted. |
| **Quick view** (drawer) | Opened from a card, a list row or the global search. Stage pill, *Avanzar a…*, *Descartar*, answers, alerts, external checks, the last messages with a **quick reply**, a **quick note**, and *Abrir ficha* for the full file. |
| **Candidatos** | Saved views as tabs (*Todos, Por revisar, Pre-calificados, Esperando respuesta, Con alertas, Mejor puntuados*). One filter row: search + chips for **Etapa**, **Resultado** and **Alerta** (popovers with checkboxes and live counts) + **Más filtros**, a collapsible panel (channel, language, score range, start dates, city, tag, owner, qualified-by-assistant, conversation owner; its open/closed state is remembered). Active filters, including those that come from an analytics drill-down (reached question, drop-off question, knockout reason, FAQ used), are shown as removable pills with a single **Limpiar filtros**. Sortable table with an inline stage pill per row, pagination, bulk move / tag, CSV export. |
| **Candidate file → Perfil** | Data collected by the assistant, each field **editable** (typed editors, validated server-side, audited, re-scored); owner, tags, interview date; internal notes; channel, contact and timing info. |
| **Candidate file → Resultados del screening** | Outcome banner (qualified / knockout reason / incomplete / **verifying** / in progress), completion, score with per-criterion breakdown, alerts and agent notes. Then one row per question: normalised answer (ID numbers masked), status (answered, knockout, unresolved, pending, missing), attempts, and **what the candidate literally said**. Next, **Verificaciones externas**: provider, status (pending / valid / invalid / error), detail, dates, and the actions *Válido* / *No válido* (manual checks or overrides) and *Reintentar* (failed checks); the card refreshes itself while a check is pending. Finally every business rule of the process with its result (met / triggered / waiting for a check / not evaluated), evaluated against **the revision that screened the candidate**. |
| **Candidate file → Conversación** | Full transcript (candidate, assistant, recruiters). **Take over** / hand back, write to the candidate, **reopen** the screening on any question. |
| **Candidate file → Actividad** | Timeline: recruiter actions (moves, notes, edits, messages, takeovers, reopen), automatic placements, rules triggered, closes, nudges. Technical agent events on demand. |
| **Analítica** | Period (7 / 30 / 90 days / all), channel and language. KPIs (candidates, in conversation, pre-qualified, knockouts, incomplete, verifying, average score, duration, completion rate), funnel per question, drop-off, pipeline distribution, knockout reasons, alerts, languages, FAQ usage. **Almost everything is clickable**: a KPI card or a chart row opens *Candidatos* with exactly the same filters (same process, period, channel, language + the clicked segment), so the list shows the same number as the chart. |
| **Procesos** | List of recruiting processes (status, published revision, unpublished changes, candidates) with *Editar* and a *⋯* menu (*Duplicar, Activar/Desactivar, Abrir chat de candidato, Eliminar* — only without candidates) and *Nuevo proceso* (role + company; the id is suggested). |
| **Procesos → Editar** | The process editor, with a **basic mode** (default) and an **advanced mode** (*Configuración avanzada* switch, remembered per browser). Basic: general settings, questions as cards (text, answer type, required, options), rules written as sentences (*If «data» «condition» «value» → discard / send to review / flag*), scoring as sentences, greeting and closing messages, FAQ, coverage zones, history. Advanced adds ids and data fields, model descriptions, alternative phrasings, thresholds, vocabularies, re-ask messages, rule triggers and flags, **external checks**, persona settings and the remaining messages. Autosaved draft with per-field problems, *Probar* (simulator drawer), *Publicar* with a note, *⋯ → Descartar borrador / Exportar JSON / Historial*. See `docs/screening_profiles.md`. |

A global **process selector** in the top bar scopes the pipeline, list and analytics to one recruiting process. **Nuevo candidato** registers a candidate by hand (referral, walk-in, phone call) and returns a personal screening link (`/?session=<id>`). The assistant skips what is already known, such as the name.

## Pipeline vs screening outcome

Two different things are shown side by side:

- **Screening outcome** (`status`): decided by the agent (`active`, `qualified`, `disqualified`, `abandoned`).
- **Pipeline stage** (`pipeline_stage`): owned by recruiters. Default stages: `screening → review → qualified → shortlisted → interview → offer → hired`, plus `rejected`. Stages live in the `pipeline_stages` table and are **editable** (label, colour, order; add/delete). `screening`, `review`, `qualified` and `rejected` are system stages: the automatic placement below needs them, so they can't be deleted. Deleting any other stage moves its candidates to a stage of your choice (logged as a `pipeline_change` event with reason `stage_deleted`).

Until a recruiter moves a candidate, the stage follows the outcome automatically (`backend/pipeline.py`):

| Outcome | Automatic stage |
|---|---|
| in progress, or verifying (answers complete, waiting for blocking external checks) | En screening |
| qualified | Pre-calificado, or **Por revisar** if a `review: true` flag fired (e.g. physical limitations), an external check failed (`check_error:*`) or there was a self-harm signal |
| disqualified | Descartado |
| abandoned | Por revisar (incomplete or silent: worth a call). Descartado if closed for abusive language. |

Once moved by hand, the stage **sticks**: later agent activity never overrides a recruiter's decision. Automatic moves are logged as `pipeline_auto` events.

## Manual interaction (human takeover)

1. The recruiter writes in *Conversación*. By default the assistant is **paused** for that candidate. The message is delivered on the candidate's channel: pushed over WhatsApp, or shown in the web chat within seconds. Voice callers are told a recruiter will follow up.
2. While paused, candidate messages are stored and shown to the recruiter; the assistant doesn't answer. The board and list show "Esperando tu respuesta". Re-engagement nudges and silence timeouts are suspended. Safety still comes first: a self-harm message is answered with helplines immediately.
3. **Devolver al asistente** hands back: the assistant thanks the candidate and re-asks the pending question.
4. **Reabrir screening** works on any candidate, even rejected or qualified ones. It clears the chosen answer (by default, the knockout) and re-asks it. Use it for misunderstandings ("perdón, sí tengo carnet") or changed circumstances.

Recruiters can also message candidates whose screening is closed ("¿Puedes venir el lunes a las 10?"). Replies reach the recruiter instead of the agent's closing message.

## Search & filters

- **Free text**: every word must match (AND), case- and accent-insensitive ("jimenez" finds "Jiménez"). Searches name, contact, city, id, collected answers, agent notes, flags, tags, owner, **recruiter notes** and **everything the candidate wrote** in the conversation.
- **Facets with live counts**: pipeline stage, screening outcome, process, channel. Plus language and alert.
- **Process** (global selector), **alert** (any flag, e.g. `physical_review`, `needs_review`), **tag**, **owner** (or unassigned), qualified-by-bot, **conversation owner** (assistant vs recruiter).
- **Ranges**: score min–max, start date from–to, city.
- **Screening progress** (also used by the analytics drill-down): `reached` (answered a question, or `qualified`), `dropped_at` (abandoned on a question), `disqualify_reason` (knockout rule), `faq` (asked a given FAQ entry).
- **Sort**: last activity, newest/oldest, score, name, city.
- Everything is reflected in the URL, and the CSV export applies the same filters. It includes every process's data columns.

## Audit & identity

Every recruiter action is an event with `by` (the recruiter's name, set once per browser and sent as `X-Recruiter`). Internal notes and recruiter data never leave through the candidate-facing `/api/sessions/{id}`.

Access control for the pilot: HTTP Basic auth on `/backoffice`, `/api/bo/*`, `GET /api/sessions` and `/api/analytics` when `BACKOFFICE_PASSWORD` is set. For production, put the portal behind SSO at the reverse proxy (per-user identity then comes from the proxy).

## API (`/api/bo`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/meta` | Pipeline stages (with colour, `system`, candidate count), stage colours, profiles (public view), labels for statuses/sources/flags, known tags and owners |
| GET | `/candidates` | Search. Query: `q, profile_id, pipeline_stage, status, source, language, flag, tag` (comma-separated lists) `, assignee (__none__), qualified, bot_paused, min_score, max_score, started_from, started_to, city, reached, dropped_at, disqualify_reason, faq, sort, order, page, page_size` → `{items, total, page, page_size, facets}` |
| GET | `/candidates.csv` | Same filters, CSV download |
| GET | `/pipeline` | Same filters, grouped by pipeline stage (50 per column + counts) |
| POST | `/candidates` | Create manually `{profile_id, name?, contact?, language?, note?}` → `{id, chat_link}` |
| GET | `/candidates/{id}` | Full file: session, transcript, events, notes, screening `report`, profile questions, pending question |
| PATCH | `/candidates/{id}` | `{pipeline_stage?, assignee?, tags?, interview_at?}` |
| PATCH | `/candidates/{id}/data` | `{changes: {field: value}}`: validated per field type, audited, re-scored |
| POST / DELETE | `/candidates/{id}/notes[/{note_id}]` | Internal notes |
| POST | `/candidates/{id}/messages` | `{text, pause_bot=true}`: message the candidate on their channel |
| POST | `/candidates/{id}/takeover` | `{paused: bool}`: pause / hand back the assistant |
| POST | `/candidates/{id}/reopen` | `{question_id?}`: reopen the screening and re-ask |
| POST | `/candidates/bulk` | `{ids, action: move\|tag\|assign, value}` |
| POST | `/candidates/{id}/verifications/{vid}/decision` | `{status: valid\|invalid, note?}`: manual check answer or override |
| POST | `/candidates/{id}/verifications/{vid}/retry` | re-queue a failed check |
| GET | `/analytics` | Metrics over the sessions matching the same search filters as `/candidates` (`profile_id, source, language, started_from, …`) |
| GET / POST | `/stages` | list pipeline stages / create `{label, color?, after?}` (inserted before *Descartado* by default) |
| PATCH / DELETE | `/stages/{id}` | `{label?, color?}` / delete `?move_to=<stage>` (required when it has candidates; system stages and invalid targets → 422) |
| PUT | `/stages/order` | `{ids: [...]}`: new order of all stages |
| GET / POST | `/profiles` | list processes / create `{id, company, role}` or duplicate `{id, from_profile}` (draft) |
| GET / DELETE | `/profiles/{id}` | editor payload (published, draft, problems, versions, editor vocabulary) / delete (no candidates) |
| PUT / DELETE | `/profiles/{id}/draft` | save the draft `{profile, base_revision}` → `{draft_revision, problems}` (409 on concurrent edit) / discard |
| POST | `/profiles/{id}/publish` | `{note?}` → new revision (422 with `problems` if invalid) |
| POST | `/profiles/{id}/active` | `{active}` (publishes a revision) |
| GET / POST | `/profiles/{id}/versions/{rev}[/restore]` | download a revision as JSON / copy it into the draft |
| POST | `/profiles/{id}/simulate` | `{messages, source: draft\|published}` → transcript, status, data, flags, score |
