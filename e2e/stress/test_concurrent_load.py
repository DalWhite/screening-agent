"""Load test — objective 3: how does the system behave under concurrency, and
does the current architecture (one uvicorn process, sync FastAPI routes run on
a thread pool, SQLite in WAL mode) support it?

~100 candidates chat at once (organic, varied scenarios: knockouts, reviews,
FAQ, bilingual, drop-offs) while ~10 recruiters work the backoffice at the
same time (board, search, quick view, stage moves, notes, analytics,
CSV export, manual candidates) through real browsers. Nothing here is mocked
except the LLM (as in the rest of the e2e suite) — same server, same SQLite
database, same background workers (re-engagement, verification) as production.

This suite is opt-in (heavier and slower than the functional e2e tests), and
skipped by default:

    python -m pytest e2e -m stress -s             # the default-sized run (~100 + 10)
    STRESS_CANDIDATES=300 STRESS_RECRUITERS=20 python -m pytest e2e -m stress -s

A JSON report (latency percentiles, throughput, error rate, server CPU/RSS
over time, data-integrity checks) is written to ``e2e/stress/reports/`` and a
summary is printed (``-s``). See ``docs/load_testing.md`` for how to read it
and what we found running it.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright

from .conftest import StressServer
from .metrics import LatencyRecorder, ProcessSampler, render_report, timed, write_report
from .personas import candidate_plan

pytestmark = pytest.mark.stress

OVERALL_TIMEOUT = float(os.environ.get("STRESS_TIMEOUT", "300"))
MAX_RECRUITERS = 24  # RECRUITER_NAMES just needs to cover the largest scenario below (or STRESS_RECRUITERS)
RECRUITER_NAMES = [f"Recruiter{n + 1}" for n in range(max(MAX_RECRUITERS, int(os.environ.get("STRESS_RECRUITERS", 0))))]

# Two points on the curve: a light one (comfortably within a normal day) and the volume asked
# about (~100 candidates / ~10 recruiters at once) — run together so the report shows how the
# system degrades between them, not just a single snapshot. STRESS_CANDIDATES/STRESS_RECRUITERS
# override *both* when set (e.g. for a quick smoke run), as a single scenario.
if os.environ.get("STRESS_CANDIDATES") or os.environ.get("STRESS_RECRUITERS"):
    SCENARIOS = [pytest.param(int(os.environ.get("STRESS_CANDIDATES", 100)),
                              int(os.environ.get("STRESS_RECRUITERS", 10)), "custom", id="custom")]
else:
    SCENARIOS = [
        pytest.param(20, 3, "baseline_20x3", id="baseline-20-candidates-3-recruiters"),
        pytest.param(100, 10, "target_100x10", id="target-100-candidates-10-recruiters"),
    ]


# --------------------------------------------------------------------------------------
# Candidates: organic chats over plain HTTP (this is all the candidate channel ever does)
# --------------------------------------------------------------------------------------


async def candidate_task(client: httpx.AsyncClient, recorder: LatencyRecorder, seed: int, i: int) -> dict[str, Any]:
    rng = random.Random(seed)
    plan = candidate_plan(rng, i)
    result: dict[str, Any] = {"plan": plan, "id": None, "ok": True, "sent": 0, "error": None}
    try:
        resp = await timed(recorder, "create_session",
                           lambda: client.post("/api/sessions", json={"profile_id": plan.profile_id, "language": plan.lang}))
        resp.raise_for_status()
        result["id"] = resp.json()["session_id"]
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=f"create_session: {type(exc).__name__}: {exc}")
        return result
    for msg in plan.messages:
        await asyncio.sleep(rng.uniform(0.02, 0.25))  # an organic typing pace, not a thundering herd
        try:
            resp = await timed(recorder, "send_message",
                               lambda m=msg: client.post(f"/api/sessions/{result['id']}/messages", json={"text": m}))
            if resp.status_code >= 500:
                result.update(ok=False, error=f"send_message: HTTP {resp.status_code}")
                return result
            result["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, error=f"send_message: {type(exc).__name__}: {exc}")
            return result
    return result


# --------------------------------------------------------------------------------------
# Recruiters: the same backoffice a human uses, in real (headless) browsers
# --------------------------------------------------------------------------------------


async def _goto(page, path: str) -> None:
    await page.goto(path, wait_until="domcontentloaded")


async def act_view_pipeline(page, base_url: str, rng: random.Random) -> None:
    await _goto(page, f"{base_url}/backoffice#/pipeline")
    await page.get_by_test_id("board").wait_for(timeout=15000)


async def act_search_candidates(page, base_url: str, rng: random.Random) -> None:
    stage = rng.choice(["screening", "review", "qualified", "disqualified", ""])
    await _goto(page, f"{base_url}/backoffice#/candidates")
    await page.get_by_test_id("results").wait_for(timeout=15000)
    if stage:
        await page.get_by_test_id("search").fill(stage[:3])
        await page.wait_for_timeout(350)  # the search box debounces


async def act_triage_a_candidate(page, base_url: str, rng: random.Random) -> None:
    await _goto(page, f"{base_url}/backoffice#/candidates")
    rows = page.get_by_test_id("candidate-row")
    await page.get_by_test_id("results").wait_for(timeout=15000)
    count = await rows.count()
    if count == 0:
        return
    await rows.nth(rng.randrange(count)).click()
    panel = page.get_by_test_id("quick-view")
    await panel.wait_for(timeout=15000)
    if rng.random() < 0.5 and await panel.get_by_test_id("advance").count():
        await panel.get_by_test_id("advance").click()
    elif await panel.get_by_label("Nota rápida").count():
        await panel.get_by_label("Nota rápida").fill("Revisado durante la prueba de carga.")
        await panel.get_by_label("Nota rápida").press("Enter")
    await page.wait_for_timeout(150)
    await panel.get_by_label("Cerrar panel").click()


async def act_view_analytics(page, base_url: str, rng: random.Random) -> None:
    await _goto(page, f"{base_url}/backoffice#/analytics")
    await page.get_by_test_id("analytics-body").wait_for(timeout=15000)
    if await page.get_by_test_id("stat-total").count():
        await page.get_by_test_id("stat-total").click()
        await page.get_by_test_id("results").wait_for(timeout=15000)


async def act_export_csv(page, base_url: str, rng: random.Random) -> None:
    await _goto(page, f"{base_url}/backoffice#/candidates")
    href = await page.get_by_test_id("export-csv").get_attribute("href")
    resp = await page.request.get(f"{base_url}{href}")
    assert resp.status == 200


async def act_create_candidate(page, base_url: str, rng: random.Random) -> None:
    await _goto(page, f"{base_url}/backoffice#/pipeline")
    await page.get_by_role("button", name="Nuevo candidato").click()
    dialog = page.get_by_role("dialog", name="Nuevo candidato")
    suffix = f"{int(time.time() * 1000) % 100000}-{rng.randint(0, 999)}"
    await dialog.get_by_label("Proceso de selección").select_option(
        rng.choice(["construction_laborer", "delivery_courier"]))
    await dialog.get_by_label("Nombre y apellidos").fill(f"Referido Carga {suffix}")
    await dialog.get_by_role("button", name="Crear candidato").click()
    await dialog.get_by_test_id("screening-link").wait_for(timeout=15000)


RECRUITER_ACTIONS = [act_view_pipeline, act_search_candidates, act_triage_a_candidate, act_view_analytics,
                     act_export_csv, act_create_candidate]
# weighted so reading the board/list (what a recruiter does most) dominates over writes
ACTION_WEIGHTS = [3, 3, 3, 2, 1, 1]


async def recruiter_task(page, base_url: str, recorder: LatencyRecorder, name: str, seed: int,
                         stop: asyncio.Event, max_iterations: int) -> dict[str, Any]:
    rng = random.Random(seed)
    done = 0
    errors: list[str] = []
    for _ in range(max_iterations):
        if stop.is_set():
            break
        action = rng.choices(RECRUITER_ACTIONS, weights=ACTION_WEIGHTS)[0]
        try:
            await timed(recorder, action.__name__, lambda a=action: a(page, base_url, rng))
        except Exception as exc:  # noqa: BLE001 - recorded; the recruiter just tries the next action
            errors.append(f"{action.__name__}: {exc}"[:200])
        done += 1
        await asyncio.sleep(rng.uniform(0.1, 0.4))
    return {"name": name, "iterations": done, "errors": errors}


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


async def _run_scenario(server: StressServer, n_candidates: int, n_recruiters: int) -> dict[str, Any]:
    sampler = ProcessSampler(server.pid)
    http_recorder = LatencyRecorder()
    ui_recorder = LatencyRecorder()
    sampler.start()
    t0 = time.monotonic()

    stop_recruiters = asyncio.Event()

    async def run_candidates() -> list[dict[str, Any]]:
        limits = httpx.Limits(max_connections=max(n_candidates, 100), max_keepalive_connections=max(n_candidates, 100))
        async with httpx.AsyncClient(base_url=server.url, timeout=httpx.Timeout(30, connect=10), limits=limits) as client:
            results = await asyncio.gather(
                *(candidate_task(client, http_recorder, 10_000 + i, i) for i in range(n_candidates)))
        stop_recruiters.set()
        return list(results)

    async def run_recruiters() -> list[dict[str, Any]]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                async def one(idx: int) -> dict[str, Any]:
                    ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                    await ctx.add_init_script(f"localStorage.setItem('bo_recruiter_name', '{RECRUITER_NAMES[idx]}')")
                    page = await ctx.new_page()
                    try:
                        return await recruiter_task(page, server.url, ui_recorder, RECRUITER_NAMES[idx],
                                                   20_000 + idx, stop_recruiters, max_iterations=200)
                    finally:
                        await ctx.close()

                return list(await asyncio.gather(*(one(j) for j in range(n_recruiters))))
            finally:
                await browser.close()

    candidate_results, recruiter_results = await asyncio.wait_for(
        asyncio.gather(run_candidates(), run_recruiters()), timeout=OVERALL_TIMEOUT)

    wall = time.monotonic() - t0
    sampler.stop()

    # let the settled state catch up: async ID checks and the (fixed 15s-tick) re-engagement
    # monitor still have background work queued for the candidates that kept the server busy
    truncated_ids = {r["id"] for r in candidate_results if r["ok"] and r["plan"].truncated and r["id"]}
    async with httpx.AsyncClient(base_url=server.url, timeout=20, headers={"X-Recruiter": "load-test"}) as admin:
        items = await _wait_settled(admin, http_recorder, n_candidates, truncated_ids)

    integrity = _check_integrity(candidate_results, items, server)

    return {
        "scenario": f"{n_candidates} concurrent candidates + {n_recruiters} concurrent recruiters",
        "candidates": _candidate_summary(candidate_results),
        "recruiters": {"count": n_recruiters, "iterations": sum(r["iterations"] for r in recruiter_results),
                       "ui_errors": sum(len(r["errors"]) for r in recruiter_results),
                       "sample_errors": [e for r in recruiter_results for e in r["errors"]][:10]},
        "wall_seconds": round(wall, 1),
        "server_pid": server.pid,
        "server": sampler.summary(),
        "http": http_recorder.summary(),
        "ui": ui_recorder.summary(),
        "data_integrity": integrity,
    }


async def _wait_settled(admin: httpx.AsyncClient, recorder: LatencyRecorder, n_candidates: int,
                        truncated_ids: set[str], timeout: float = 55.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        items = []
        page = 1
        while True:  # the admin API caps page_size at 200: page through it for larger runs
            resp = await timed(recorder, "bo_candidates_poll",
                               lambda p=page: admin.get("/api/bo/candidates", params={"page_size": 200, "page": p}))
            data = resp.json()
            items.extend(data["items"])
            if len(items) >= data["total"] or not data["items"]:
                break
            page += 1
        by_id = {i["id"]: i for i in items}
        nobody_verifying = all(i["status"] != "verifying" for i in items)
        # silent candidates only close once genuinely idle (re-engagement's fixed 15s tick): give
        # them the time the server itself is configured to wait, not just "no longer verifying"
        truncated_settled = all(by_id.get(sid, {}).get("status") != "active" for sid in truncated_ids)
        if len(items) >= n_candidates and nobody_verifying and truncated_settled:
            break
        await asyncio.sleep(2)
    return items


def _candidate_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_scenario = Counter(r["plan"].scenario for r in results)
    failed = [r for r in results if not r["ok"]]
    return {
        "count": len(results),
        "fully_sent": sum(1 for r in results if r["ok"]),
        "http_failures": len(failed),
        "by_scenario": dict(sorted(by_scenario.items())),
        "sample_failures": [f"{r['plan'].label} ({r['plan'].scenario}): {r['error']}" for r in failed[:10]],
    }


def _check_integrity(results: list[dict[str, Any]], items: list[dict[str, Any]], server: StressServer) -> dict[str, Any]:
    problems: list[str] = []
    by_id = {i["id"]: i for i in items}
    expect_status = {"qualified": "qualified", "disqualified": "disqualified", "abandoned": "abandoned"}
    mismatches = 0
    truncated_total = truncated_abandoned = 0
    for r in results:
        if not r["ok"] or r["id"] is None:
            continue
        plan = r["plan"]
        row = by_id.get(r["id"])
        if row is None:
            problems.append(f"{plan.label}: session vanished from the candidate list")
            continue
        if row["status"] == "verifying":
            problems.append(f"{plan.label}: still 'verifying' after the settle window")
        if plan.truncated:
            # left mid-conversation on purpose: only the background re-engagement monitor (on its
            # own fixed 15s tick) can close these, so check the *aggregate* outcome, not each one
            truncated_total += 1
            truncated_abandoned += row["status"] == "abandoned"
            continue
        want = expect_status.get(plan.expect_status)
        if want and row["status"] != want:
            mismatches += 1
            if mismatches <= 10:
                problems.append(f"{plan.label} ({plan.scenario}): expected status={want}, got {row['status']!r}")
    if truncated_total and truncated_abandoned < truncated_total * 0.6:
        problems.append(f"only {truncated_abandoned}/{truncated_total} silent candidates were closed as "
                        f"'abandoned' by the re-engagement monitor within the settle window")
    if len({i["id"] for i in items}) != len(items):
        problems.append("duplicate session ids in the candidate list")
    log_text = server.log_path.read_text(errors="replace") if server.log_path.exists() else ""
    tracebacks = len(re.findall(r"Traceback \(most recent call last\)", log_text))
    locked = len(re.findall(r"database is locked", log_text, re.IGNORECASE))
    if tracebacks:
        problems.append(f"{tracebacks} unhandled exception(s) in the server log (see {server.log_path})")
    return {"problems": problems, "mismatches": mismatches, "server_tracebacks": tracebacks,
            "database_locked_errors": locked, "truncated_abandoned": f"{truncated_abandoned}/{truncated_total}",
            "server_log": str(server.log_path)}


# --------------------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("n_candidates, n_recruiters, report_name", SCENARIOS)
def test_concurrent_candidates_and_recruiters(stress_server: StressServer, reports_dir: Path, n_candidates: int,
                                              n_recruiters: int, report_name: str) -> None:
    report = asyncio.run(_run_scenario(stress_server, n_candidates, n_recruiters))
    write_report(report, reports_dir / f"{report_name}.json")
    print(render_report(report))

    # ---- Correctness under load: non-negotiable regardless of how slow it gets. A single
    # process + SQLite must never lose a message, corrupt a candidate's state or crash. ----
    assert stress_server  # the fixture would already have raised if the server process had died
    overall = report["http"]["_overall"]
    assert overall["error_rate_pct"] < 5, f"too many failed candidate requests under load: {overall}"
    # a per-request error rate this low still compounds over a multi-turn conversation (~9 requests:
    # even a 1.5% per-request failure rate leaves ~12% of candidates with an interrupted chat), so the
    # completion bar is deliberately looser than the request error-rate bar — see docs/load_testing.md
    assert report["candidates"]["fully_sent"] >= report["candidates"]["count"] * 0.8, report["candidates"]
    assert report["data_integrity"]["server_tracebacks"] == 0, report["data_integrity"]
    assert report["data_integrity"]["database_locked_errors"] == 0, report["data_integrity"]
    assert report["data_integrity"]["mismatches"] <= max(2, report["candidates"]["count"] * 0.03), \
        report["data_integrity"]
    got, _, total = report["data_integrity"]["truncated_abandoned"].partition("/")
    if int(total or 0) >= 5:  # background workers (re-engagement) keep working under load
        assert int(got) >= int(total) * 0.6, report["data_integrity"]

    # ---- Responsiveness under load: *not* a correctness bar, just "didn't fall over" — the
    # single-process/sync-route/thread-pool design is expected to queue (and recruiters to see
    # slow pages or the odd timeout) well before it would ever drop or corrupt data. A high
    # number here is the actual answer to "does this scale to N concurrent users": read the
    # printed latency table (and docs/load_testing.md) rather than only this assertion. ----
    ui_error_rate = report["recruiters"]["ui_errors"] / max(1, report["recruiters"]["iterations"])
    assert ui_error_rate < 0.5, (f"recruiters could not even mostly use the backoffice under this load "
                                 f"({ui_error_rate:.0%} of actions failed/timed out): {report['recruiters']}")
