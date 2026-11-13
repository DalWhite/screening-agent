# Candidate identity, lifecycle and idempotency — design

Status: **P1 + P2 implemented** (W1–W3, §7.3); option B of §7.4 (shared WhatsApp number, sequential
applications) is decided and comes next. Acceptance: `python -m pytest -m identity` → 42 passed
(40 scenarios + 2 guards), 17 deferred. Implementation notes in §9.

## 0. Scope

- **One application instance** (a monolith, one process). In-process locks are valid; multi-worker
  correctness is deferred.
- **WhatsApp and phone** are the candidate channels. The web chat and the session API are a test
  harness: no security or identity work on them. The backoffice stays in scope (referrals, recruiter
  messages, reopen).
- **Out of scope:** W4 (multi-worker state, monitor locking) and W5 (analytics, retention).
- **Scenario tests:**

  | | Count | State |
  |---|---|---|
  | In scope | 40 | Failing until implemented: 38 from the first round + 2 on parallel applications (§7) |
  | Deferred | 17 | Skipped with reasons in `tests/support/identity.py` (`DEFERRED_*`) |
  | Guards | 2 | Pass today and must keep passing |

- **Decisions:** D1–D4, D6, D7, D9 approved with the recommended defaults (§5). D5 and D8 are moot with
  this scope.

## 1. Principles

1. **A person is not a session.** A `candidate` owns *applications* (today's sessions). Sessions keep
   everything they have; they gain a `candidate_id`.
2. **Identity keys are not channel addresses.** `sessions.contact` stays the address a channel writes to
   (WhatsApp's `+521…`); identity uses a canonical key (`+52…`).
3. **Strong and weak keys.** A phone number proves possession of a line, not who you are (shared and
   recycled numbers); an ID document is strong. Keys link records; they never copy data between people.
4. **One live application per candidate and process.** Several processes at once are allowed (§7).

## 2. Workstreams

### W1 — Identity model

**Schema** (additive migration):

```sql
CREATE TABLE candidates (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    merged_into TEXT REFERENCES candidates(id),
    merged_at TEXT,
    channels_json TEXT NOT NULL DEFAULT '{}'  -- per channel: address, WhatsApp window, focus (§7)
);
CREATE TABLE candidate_keys (
    kind TEXT NOT NULL,                 -- phone | id_document
    value TEXT NOT NULL,                -- canonical phone | HMAC hex
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    verified INTEGER NOT NULL DEFAULT 0,  -- possession proven (inbound WhatsApp, caller ID) vs claimed
    source TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
ALTER TABLE sessions ADD COLUMN candidate_id TEXT;
CREATE INDEX idx_sessions_candidate ON sessions(candidate_id);
CREATE UNIQUE INDEX uq_live_application ON sessions(candidate_id, profile_id)
    WHERE status IN ('active', 'verifying');   -- safety net behind the in-process lock
```

Candidates hold no personal data: names and cities come from their applications.

**`backend/identity/`**

- `phones.canonical_phone(raw, default_region="ES") -> str | None`
  - strips `whatsapp:`, `tel:` and separators; `00` → `+`;
  - `None` for withheld caller IDs (`anonymous`, `restricted`, `unknown`, `private`, `unavailable`,
    `blocked`, Twilio's `+266696687`, `+7378742833`, `+2562533`, `+8656696`, `+86282452253`, anything
    non-numeric);
  - Spanish national numbers (9 digits, 6/7/8/9) → `+34…`; other national numbers → `None`
    ("add the country code");
  - WhatsApp quirks: Mexico `+521` + 10 digits → `+52` + 10 digits.
- `keys.document_key(number)`: HMAC-SHA256 with `IDENTITY_KEY_SECRET` from the environment
  (dev fallback: a generated `data/identity.key`, gitignored).
- `IdentityService`
  - `candidate_for(kind, value, verified, source)`: get-or-create under an in-process lock per key,
    shared by every channel of the hub. Follows `merged_into`. A phone key unseen for 365 days (D3)
    no longer links: the line may have been recycled.
  - `attach(session, kind, value)`: the ID document typed in a turn (`decision.secrets`, in
    `persist_turn`). It is linked before the registry answers and flagged unverified until then.
    A key owned by another candidate → `merge`.
  - `merge(a, b)`: the older candidate survives; sessions and keys are repointed; event
    `identity_merge`. If both have a live application in the same process, the newer one is closed
    with the new terminal status **`duplicate`** (D6: review stage, excluded from funnel numbers,
    candidate told a recruiter will review).
  - Withheld numbers and web sessions get an anonymous candidate with no keys: nobody else can ever
    resolve to it.

**Key sources**

| Source | Key |
|---|---|
| WhatsApp inbound | verified phone |
| Caller ID (not withheld) | verified phone |
| Recruiter referral | claimed phone |
| `id_document` answer | document HMAC |

### W2 — Application lifecycle

`BaseChannel.resolve_session` becomes candidate-based (WhatsApp and phone share it):

```
key = canonical_phone(contact)          # None: withheld -> new application, anonymous candidate
with identity_lock(key):
    cand = identity.candidate_for("phone", key, verified=True)
    focus = cand's focus on this channel, if live (§7)
    apps = [focus] + applications(cand, this line's process), newest first
           (+ this channel's latest application elsewhere, only if that process is still active)
    live & fresh                                         -> continue
    live but stale (no activity for the resume window;
                    never-answered referral: 14 days)    -> close (abandoned, "expired"), keep looking
    abandoned inside the resume window                   -> resume (as today)
    decided inside FOLLOW_UP_DAYS (30, D1)               -> follow-up on the decided application
    otherwise                                            -> new application of cand
```

- **Follow-up after a decision.** Routing the channel back to the decided application is enough: the
  engine already listens silently during a takeover and otherwise answers with the closing message
  (`ignored_after_close`). New profile message `after_decision` per outcome (falls back to `review`),
  at most once every few hours. Phone: takeover greeting, or a new after-decision greeting that takes a
  message. Backoffice: `awaiting_reply` also for decided applications with a new candidate message.
- **Re-application** after 30 days: a new application of the same candidate. Before that, a knockout
  can't be gamed; a recruiter can reopen.
- **Never-answered applications** are closed by the monitor after the resume window (D7); referrals
  wait 14 days. This is the only monitor change, part of the lifecycle rules, not W4.
- **Inactive processes** are never continued from a channel.
- **Reopen guard:** refused when the candidate has another live application in that process.
- **Referrals:** `_clean_contact` uses `canonical_phone` (ambiguous → 422). A referral for a candidate
  with a live application in that process returns the existing one (`existing: true`).

### W3 — Phone-provider idempotency

Providers retry webhooks. One process does not make retries disappear. WhatsApp is already covered
in-process (`_seen` plus `channel_inbound`), so only the phone needs work:

```sql
CREATE TABLE idempotency (
    scope TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL,   -- processing | done
    response_json TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
```

- **`voice:start` / call id** (Twilio `CallSid`, Vonage `uuid`). The first `VoiceAction` is stored. A
  retry gets the same answer, so `voice_call_started` is logged once. A concurrent duplicate waits for
  the first (bounded) and then replays it.
- **`voice:turn` / `{call id}:{token}`**. `VoiceAction` carries a per-prompt token that the adapters
  append to the collect / event URL (`&turn=`). A replayed speech result reuses the URL, so it is
  processed once. Requests without a token behave as today, so existing URLs and tests keep working.
- Rows older than a day are deleted opportunistically (no retention job).

A database table rather than a dict: a retry that arrives right after a restart is still recognised.

## 3. Scenario → solution (40 in scope)

| Failing tests (count) | Solution |
|---|---|
| Retried / concurrent call start, replayed call webhook (3) | W3 `voice:start` |
| Replayed speech result (1) | W3 `voice:turn` |
| Withheld caller IDs (8) | W1 `canonical_phone` → anonymous candidate |
| `0034` / `00 52` referral formats (2), referral without country code (1) | W1 `canonical_phone` in `_clean_contact` |
| Deactivated process (1) | W2 |
| Months-old application, never-answered application (2) | W2 expiry |
| Mexican numbers, unit + integration (4) | W1 canonical key |
| Decided candidate writes / calls / answers a recruiter (6) | W2 follow-up |
| Decision not reopened from another channel (2), knockout re-application (2), reopen with a newer live application (1) | W2 follow-up + reopen guard |
| After the resume window, two processes (2) | W1 phone key |
| Same ID document from two numbers (1) | W1 `attach` + `merge` |
| Referral registered twice (1) | W2 |
| WhatsApp and phone at the same moment (1) | W1 identity lock + `uq_live_application` |
| Recruiter message about another application reaches WhatsApp, reply routed to it (2) | §7 candidate-level reach + focus |

Guards that must stay green: a shared number never overwrites the first person's screening, and the
ID number is never stored in clear or as a plain hash.

## 4. Phasing

| Phase | Content | Tests turning green |
|---|---|---|
| **P1** — no identity model | W3; `canonical_phone` (withheld, referral formats); inactive processes; expiry of stale / never-answered applications | 18 |
| **P2** — identity | Schema + migration; candidate-based resolution; follow-up; re-application; reopen guard; referral dedupe; document merge; Mexican numbers; §7 reach + focus | 22 |

**Migration (P2)**
1. Backfill one candidate per canonical phone (no usable number → its own candidate).
2. Close older duplicate live applications per (candidate, process) as abandoned (`superseded`,
   audited) before creating `uq_live_application`.
3. Mark historical links `linked_by_migration`.

**Collateral updates**
- The e2e test `test_a_rejected_candidate_who_calls_again_starts_over` (becomes a follow-up).
- Docs: `channels.md`, `architecture.md`.
- `requirements.txt` + `python-multipart` (missing today).

## 5. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Follow-up window after a decision | 30 days, per-profile override — approved |
| D2 | Auto-link on phone and ID document, never merge data | approved |
| D3 | Phone key maximum age (recycled numbers) | 365 days — approved |
| D4 | Default region for national numbers | ES — approved |
| D5 | `contact` on the session API | moot (web is a test harness) |
| D6 | New terminal status `duplicate` | approved |
| D7 | Never-answered expiry | 48h, referrals 14 days — approved |
| D8 | Analytics | moot (W5 deferred) |
| D9 | Order | P1 → P2 — approved |
| **D10** | Parallel applications on one WhatsApp number (§7.4) | **B** (shared line, sequential) — approved, not implemented yet |

## 6. Trade-offs (in scope)

| Decision | Chosen | Cost / risk |
|---|---|---|
| Person model | `candidates` + `candidate_keys`; sessions stay applications | Migration and merge logic; no personal data duplicated |
| Linking | Auto-link by phone and document; records linked, data never copied | Shared or recycled numbers link two people: mitigated by the 365-day key age, a name-conflict flag and (later) a split action |
| After a decision | 30-day follow-up, then a new linked application | A genuine re-applicant waits or needs a reopen; a sibling can't apply from a shared phone in the window |
| Phone rules | In-house ES/MX rules behind one function | Must be extended per new market (`phonenumbers` is the swap-in) |
| Key vs address | Canonical key separate from `contact` | Two notions, but outbound sends keep working |
| Document key | HMAC, linked before the registry answers | Secret management; a check-letter-valid typo (1/23) mis-links until the registry answers |
| `duplicate` status | New terminal status | One more label and filter; honest funnel numbers |
| Phone idempotency | Database table | One write per call / turn; survives a restart |

## 7. Parallel applications (one candidate, several processes at once)

### 7.1 What happens today (verified)

Each line runs one process (`WHATSAPP_PROFILE`, `VOICE_PROFILE`). So parallel applications only exist
when the two lines run different processes, e.g. courier on WhatsApp and construction by phone:

- **Unlinked.** Two separate candidates. Fixed by W1: same phone key, same candidate.
- **Unreachable.** A recruiter message for the phone application is never pushed: that application
  "never used WhatsApp". The WhatsApp line only delivers the courier application's queue, so the
  message stays queued for ever.
- **Misrouted.** The candidate's WhatsApp answer to the construction recruiter ("la 42", a boot size)
  is consumed by the courier screening as their city. A follow-up ("quiero seguir con lo de la obra")
  then produced an **out-of-area knockout** in the courier process.
- **No way to apply to a second process on the same WhatsApp number.**

### 7.2 Is it desirable?

Yes. In low-qualification hiring, candidates apply to whatever is open, and the same company (or
agency) runs several processes. Recruiters benefit from seeing the other applications. The data model
already allows it: uniqueness is per candidate *and process*. The difficulty is the conversation: one
WhatsApp chat, several screenings.

### 7.3 Rules that hold whatever the routing model (in scope, tests written)

1. **Reach is per candidate, not per application.** A push for any application uses the channels the
   *candidate* has used, with the per-channel address and the WhatsApp 24h window stored on the
   candidate (`candidates.channels_json`). The window is per person-business pair, not per application.
2. **Labelled pushes.** With more than one live application, every push names the job
   ("Peón de obra · Constructora Horizonte: …").
3. **Conversation focus per channel** (`channels_json[channel].focus`). Inbound messages go to the
   application in focus, which is:
   - the application of a quoted reply, when the provider says so (Meta `context.id`, Twilio
     `OriginalRepliedMessageSid` → delivery row → application); otherwise
   - the application of the last business-initiated message that expects an answer (a recruiter
     message, a takeover); otherwise
   - the application the candidate was answering.

   When the recruiter hands back, focus returns to the interrupted screening with a
   "seguimos con tu candidatura de repartidor" re-ask.
4. **Timers follow focus.** Only the focused application is nudged. An unfocused live application is
   neither nudged nor closed for silence while the candidate is active on another one.
5. **Knockouts are per process.** Never propagated automatically: "17 años" in one process does not
   reject the other. The other recruiter sees it in the candidate file.
6. **Backoffice:** one card and pipeline position per application; the candidate file lists the other
   applications and their status.

### 7.4 Open decision D10 — several processes on one WhatsApp number

| Option | How | For | Against |
|---|---|---|---|
| **A. One line per process** (today + §7.3) | Each process gets its own WhatsApp number / phone number | Simplest; no routing ambiguity at entry | Every number needs Meta approval and a template; candidates juggle numbers; a job ad per number |
| **B. Shared line, sequential** *(recommended)* | Entry by job code in the ad's link (`wa.me/<n>?text=Hola%20[OBRA-01]`) or a WhatsApp list menu. One screening in focus at a time; a second request is queued ("terminamos esta y seguimos con Peón de obra") and offered when the first ends. The queued application waits without timers | One number; no interleaved questions; every message goes to exactly one screening | A small entry/switch intent before the screening turn; queued applications wait |
| **C. Shared line, free switching** | Like B, but the candidate can switch mid-screening ("quiero hacer la de obra") | Most flexible | Interleaved screenings in one chat: confusing, and the misrouting risk comes back; hard to explain |

Phone: one number per process stays natural (phone numbers are cheap). A shared phone line would ask
at call start which application to continue when there are several live ones ("¿repartidor u obra?").

**Later optimisation:** reuse stable answers across applications as a pre-fill with confirmation (the
name; never knockouts). The ID document can't be reused (only its HMAC is kept), but a `valid`
registry check for the same document key could be, which saves a check.

### 7.5 Scenarios to write once D10 is decided (option B)

- A job code in the first message starts that process; no code and several open processes → the menu.
- Asking for another job mid-screening queues it; it starts when the first screening ends.
- A queued application is not nudged nor closed while the other one is in progress.
- A quoted reply to a recruiter message goes to that message's application even if another is in focus.
- When the recruiter hands back, the interrupted screening resumes where it was.
- A knockout in one process leaves the other untouched.
- A call with two live applications asks which one to continue.

## 8. Deferred (tests written, skipped)

| Area | Tests | Why deferred |
|---|---|---|
| Multi-worker | WhatsApp claim race, stale claim window, retry on another worker, first contact on two workers, stale copy on another worker, takeover on another worker, double nudges (7) | One instance |
| W4 monitor (single instance, parked with W4) | Monitor closing a screening mid-turn; more than 500 active screenings after a restart (2) | Worth bringing back: both happen in a monolith, and the fixes are small |
| W5 | Analytics people count ×2, 5000 cap, retention (4) | Out of scope |
| Web / session API | `Idempotency-Key` ×2, `contact` on the API ×2 (4) | Test harness only |

## 9. Implementation notes (P1 + P2)

| Piece | Where |
|---|---|
| Canonical phone numbers, withheld caller IDs | `backend/identity/phones.py` |
| Candidates, keys, merges, per-channel facts (address, WhatsApp window, focus), document HMAC | `backend/identity/service.py` (SQL in `backend/storage/db.py`) |
| Schema, backfill of existing sessions, closing of duplicate live applications, `uq_live_application` | `db.init_db` |
| Every saved session has a candidate (the state's, the stored one, its phone's, or a new anonymous one; merges followed) | `db.save_session` |
| Candidate-based resolution, expiry of stale applications, follow-up | `BaseChannel.resolve_session` / `_applications_of` / `_continue` |
| Document linking after a turn (merge; `duplicate` closing) | `channels/base.py: link_identity` (WhatsApp and phone turns; not the web harness) |
| Reach the person, labelled pushes, focus on push, person-level 24h window | `MessagingChannel.address_of` / `_labelled` / `last_inbound` |
| Phone idempotency (`voice:start`, `voice:turn` token in the answer URL) | `VoiceChannel._once`; Twilio / Vonage adapters |
| After-decision call greeting; closing note for closed applications | `channels/voice.py`, `screening/agent.py` |
| Referral normalisation (ambiguous → 422), dedupe, reopen guard, other applications in the candidate file | `backoffice/service.py` |
| Never-answered applications closed (`expired`) | `reengagement.py` |

Deviations from the proposal:

- The per-message notice throttling ("at most once every few hours") is not implemented: a candidate
  writing several times in a follow-up gets the closing note each time.
- Per-application timers following focus (§7.3.4) come with option B.
- The web chat and session API do not link identities (test harness scope).
- Test data: the channel e2e helpers give every construction candidate their own ID document
  (`tests/support/candidates.py: unique_dni`). Sharing one now means being the same person.
