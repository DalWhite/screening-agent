# Load testing: does this architecture support 100 concurrent candidates and 10 recruiters?

`e2e/stress/` answers that question with a real, running server instead of a guess: ~100
candidates chat at once (organic, varied scripts — knockouts, reviews, FAQ, bilingual, drop-offs)
while ~10 recruiters work the backoffice at the same time (board, search, quick view, stage moves,
notes, analytics, CSV export, manual candidates), all through the same HTTP API and the same
browser UI the functional e2e suite uses. Nothing is mocked except the LLM (as everywhere else in
the test suite) — same single uvicorn process, same SQLite database (WAL mode), same background
workers (re-engagement monitor, verification worker) as a real deployment.

It is **opt-in** and excluded from both `python -m pytest` and `python -m pytest e2e` (it's slower
and heavier than a functional test, and its purpose is measurement, not day-to-day regression
checking):

```bash
pip install -r requirements-dev.txt            # adds psutil (CPU/RSS sampling)
python -m pytest e2e/stress -m stress -s        # baseline (20 candidates / 3 recruiters), then the
                                                 # target load (100 / 10); ~2 minutes, prints a report
STRESS_CANDIDATES=300 STRESS_RECRUITERS=25 python -m pytest e2e/stress -m stress -s   # go further
```

`-s` is what makes pytest print the report to the terminal; the same data is written as JSON to
`e2e/stress/reports/<scenario>.json` (checked into the repo so a diff shows whether a later change
made things better or worse). `e2e/stress/reports/baseline_20x3.json` and `target_100x10.json` are
one captured run each on a 4-core sandbox machine — expect different absolute numbers on different
hardware, but the same *shape*.

## What it checks, and how

| File | Role |
|---|---|
| `e2e/stress/conftest.py` | A dedicated server per scenario (own port, own throwaway SQLite file), with re-engagement timings shortened (first nudge 6s, second 12s, idle-close 20s) so silent candidates actually close within the test instead of needing the production 45s/120s/5min. |
| `e2e/stress/personas.py` | Randomised, organic candidate scripts (plain chat message lists) — more than a dozen scenario shapes (qualified, qualified-with-flag, every knockout, the async ID registry answering valid/not-found/down, 3-re-ask drop-off, mid-conversation drop-off, bilingual, FAQ interjections) — reused by every run, no fixed scenario list to "play against". |
| `e2e/stress/metrics.py` | `ProcessSampler` (psutil on the server's PID, sampled every 0.25s in a background thread: CPU%, RSS, thread count, inet connections) and `LatencyRecorder` (per-call latency/percentiles/throughput/error-rate, grouped by endpoint/action), independent of the scenario logic. |
| `e2e/stress/test_concurrent_load.py` | The scenario: 100 `httpx.AsyncClient` candidates (`asyncio.gather`, no browser — that's all the candidate channel ever does) + 10 real headless-Chromium recruiters (Playwright's async API, one browser / 10 contexts) running concurrently; then correctness checks against the live data. |

### Correctness bars (hard failures — these should never trip, *however slow* the system gets)

- The server process is still running and logged **no unhandled exception** and **no "database is
  locked"** anywhere (SQLite's single-writer rule is enforced with a 10s busy-timeout in
  `backend/storage/db.py`; a lock error would mean that budget was exceeded).
- Fewer than 5% of individual HTTP requests failed.
- At least 80% of candidates who had a full script sent every message successfully. (This bar is
  *below* the request error-rate bar on purpose: a conversation is ~9 sequential requests, so even
  a small per-request failure rate compounds — see "What we actually found" below.)
- Every candidate who finished their script reached the **status the engine's rules say it should**
  (qualified / disqualified with the right reason / stuck on an async check that never resolved) —
  this is compared against the live `/api/bo/candidates` data after the run, so a race that silently
  corrupted one candidate's outcome would fail the test.
- Candidates who went silent on purpose ("abandon" / "review\_given\_up" scenarios) are actually
  picked up and closed by the re-engagement monitor — proving the *background* workers keep making
  progress while the request path is under load, not just the request path itself.

### Responsiveness ("did it fall over", not an SLA)

Recruiter UI actions get a generous timeout (15-30s) before being counted as failed; the assertion
only checks that recruiters could still get *most* of their actions through (< 50% failing). The
real answer to "is it fast enough" is the printed latency table, not this pass/fail line — read the
numbers below, and decide what's acceptable for your own deployment.

## What we actually found (4-core sandbox, default MockLLM)

|  | 20 candidates / 3 recruiters | 100 candidates / 10 recruiters |
|---|---|---|
| `send_message` p50 / p95 / p99 | 44ms / 210ms / 366ms | 1.2s / 4.6s / 6.1s |
| `create_session` p50 / p95 | 216ms / 280ms | 758ms / 1.05s |
| HTTP error rate | 0% | 0% (occasionally 1-2%: see below) |
| Recruiter page loads (pipeline/candidates) | sub-second to ~2s | **2-16s**, some 15-30s timeouts |
| Server CPU (% of the 4-core machine) | ~13-25% avg/peak | ~8-55% avg/peak (never saturated) |
| Server RSS | 93 → 146 MB | 93 → 240 MB |
| Threads in the server process | peaked at 23 | **peaked at 42-46, consistently** |
| Data integrity | OK | OK — 0 tracebacks, 0 lock errors, 0 mismatches, every run |

Conclusions, in order of how confident they are:

1. **Correctness holds.** Across many runs at 100/10 concurrency, the server never crashed, never
   logged an exception, never deadlocked on SQLite, and never produced a wrong outcome for a
   candidate. A single uvicorn process + SQLite in WAL mode is safe at this volume — it just isn't
   fast.
2. **The bottleneck is the sync-route thread pool, not the CPU or SQLite locking.** The server's
   thread count reliably plateaus at ~42-46 regardless of how many more candidates are added
   (tested up to 300), which is the signature of Starlette's default thread-pool capacity for
   `def` (non-`async def`) routes (every route in `backend/app.py` and `backend/backoffice/router.py`
   except the Twilio/verification-callback webhooks is a plain `def`, so every request is queued
   onto that pool). CPU usage stays well under 100% of one core on average — the server is mostly
   *waiting* (for a free thread, or for SQLite's single writer), not computing.
3. **That queueing is exactly what turns into the latency jump.** `send_message` p50 goes from 44ms
   to 1.2s (27×) and p95 from 210ms to 4.6s (22×) between the two scenarios — consistent with
   requests sitting in the thread-pool queue rather than the per-request work getting slower.
4. **It compounds across a multi-turn conversation.** A ~1-2% per-request failure rate (occasional
   `httpx.ReadError`s with *no* matching error in the server log — i.e. the server never saw or
   failed the request; something in the connection path dropped it under load) turns into ~10-15%
   of *candidates* having an interrupted chat, because each one is ~9 sequential requests. This is
   the main reason the "fully sent" bar is looser than the request error-rate bar.
5. **Recruiters feel it first and worst.** Their actions are read-heavy (`/api/bo/candidates`,
   `/api/bo/pipeline`, `/api/bo/analytics`) competing for the *same* thread pool and the *same*
   SQLite file as 100 candidates' writes; at 100/10 some backoffice pages took 15-30+ seconds, well
   past what a recruiter would wait for.
6. **Background workers degrade gracefully.** The re-engagement monitor and the verification
   worker are asyncio tasks on the event loop (not the thread pool), so they kept running and
   closing idle candidates throughout every run — slower under load, but never stuck.

## If 100 concurrent candidates / 10 recruiters is a real target

None of this requires a rewrite; in rough order of effort:

1. **Run uvicorn with multiple workers** (`--workers N`, or a process manager in front) — cheap,
   immediately spreads the thread-pool limit across N processes. SQLite's single-writer file would
   then be the next thing to watch (WAL mode already lets readers and the writer overlap; `db.py`'s
   10s busy-timeout is the safety net, and it was never hit in testing up to 300 candidates).
2. **Move the hot, simple routes to `async def`** (health, read-only GETs that don't touch SQLite
   on the request path) so they don't consume a thread-pool slot at all.
3. **If SQLite's single writer becomes the limit** (this wasn't observed even at 300 concurrent
   candidates, but would show up as `database_locked_errors` in a future report): move to Postgres,
   or batch/serialise writes through a single worker task instead of one connection per request.
4. **Re-run this suite after any of the above** with the same `STRESS_CANDIDATES`/`STRESS_RECRUITERS`
   and compare `e2e/stress/reports/*.json` — that's what this tool is for.
