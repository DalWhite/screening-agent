"""Measurement helpers for the load tests: server resource usage and request latency.

Nothing here talks to the application; it only watches it from the outside
(``psutil`` on the server's PID) or times the HTTP/UI calls the tests already
make. Kept dependency-light on purpose (stdlib + psutil) so it can run
anywhere the rest of the e2e suite runs.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import psutil

T = TypeVar("T")


def _pctl(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


# --------------------------------------------------------------------------------------
# Server resource usage (CPU / memory / threads / connections), sampled in the background
# --------------------------------------------------------------------------------------


@dataclass
class Sample:
    t: float
    cpu_pct: float
    rss_mb: float
    threads: int
    connections: int


class ProcessSampler:
    """Samples ``psutil`` for a PID (and its children, e.g. a future multi-worker deployment)
    every ``interval`` seconds on a background thread, from outside the process under test."""

    def __init__(self, pid: int, interval: float = 0.25):
        self.root = psutil.Process(pid)
        self.interval = interval
        self.samples: list[Sample] = []
        self.cpu_count = psutil.cpu_count() or 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def _procs(self) -> list[psutil.Process]:
        try:
            return [self.root, *self.root.children(recursive=True)]
        except psutil.NoSuchProcess:
            return []

    def start(self) -> None:
        for p in self._procs():
            try:
                p.cpu_percent(None)  # first call always returns 0.0: prime it before sampling
            except psutil.NoSuchProcess:
                pass
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="process-sampler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cpu = rss = 0.0
            threads = connections = 0
            for p in self._procs():
                try:
                    with p.oneshot():
                        cpu += p.cpu_percent(None)
                        rss += p.memory_info().rss
                        threads += p.num_threads()
                        try:
                            connections += len(p.net_connections(kind="inet"))
                        except (psutil.AccessDenied, NotImplementedError):
                            try:
                                connections += len(p.connections(kind="inet"))  # psutil < 6.0
                            except (psutil.AccessDenied, NotImplementedError, AttributeError):
                                pass
                except psutil.NoSuchProcess:
                    continue
            self.samples.append(Sample(time.monotonic() - self._t0, cpu, rss / 1e6, threads, connections))
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"samples": 0}
        cpu = [s.cpu_pct for s in self.samples]
        rss = [s.rss_mb for s in self.samples]
        return {
            "samples": len(self.samples),
            "duration_s": round(self.samples[-1].t, 1),
            "cpu_cores_visible": self.cpu_count,
            # raw psutil semantics: 100% == one full core busy; can exceed 100 with several threads/cores
            "cpu_pct_of_one_core_avg": round(statistics.fmean(cpu), 1),
            "cpu_pct_of_one_core_max": round(max(cpu), 1),
            "cpu_pct_of_machine_avg": round(statistics.fmean(cpu) / self.cpu_count, 1),
            "cpu_pct_of_machine_max": round(max(cpu) / self.cpu_count, 1),
            "rss_mb_start": round(rss[0], 1),
            "rss_mb_end": round(rss[-1], 1),
            "rss_mb_max": round(max(rss), 1),
            "rss_growth_mb": round(rss[-1] - rss[0], 1),
            "threads_start": self.samples[0].threads,
            "threads_max": max(s.threads for s in self.samples),
            "inet_connections_max": max((s.connections for s in self.samples), default=0),
            "timeline": [{"t": round(s.t, 1), "cpu_pct_of_machine": round(s.cpu_pct / self.cpu_count, 1),
                         "rss_mb": round(s.rss_mb, 1), "threads": s.threads} for s in self.samples[::max(1, len(self.samples) // 60)]],
        }


# --------------------------------------------------------------------------------------
# Request latency / throughput / error rate, recorded as the tests make calls
# --------------------------------------------------------------------------------------


@dataclass
class _Req:
    label: str
    t: float
    ms: float
    status: int
    ok: bool
    error: str = ""


@dataclass
class LatencyRecorder:
    reqs: list[_Req] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    t0: float = field(default_factory=time.monotonic)

    def record(self, label: str, ms: float, status: int, ok: bool, error: str = "") -> None:
        with self.lock:
            self.reqs.append(_Req(label, time.monotonic() - self.t0, ms, status, ok, error))

    def errors(self, limit: int = 20) -> list[str]:
        with self.lock:
            return [f"{r.label} [{r.status}] {r.error}" for r in self.reqs if not r.ok][:limit]

    def summary(self) -> dict[str, Any]:
        with self.lock:
            reqs = list(self.reqs)
        by_label: dict[str, list[_Req]] = defaultdict(list)
        for r in reqs:
            by_label[r.label].append(r)
        out: dict[str, Any] = {}
        for label, items in sorted(by_label.items()):
            ms = sorted(r.ms for r in items)
            span = max((r.t for r in items), default=0) - min((r.t for r in items), default=0)
            out[label] = {
                "count": len(items),
                "errors": sum(1 for r in items if not r.ok),
                "p50_ms": round(_pctl(ms, 50), 1),
                "p90_ms": round(_pctl(ms, 90), 1),
                "p95_ms": round(_pctl(ms, 95), 1),
                "p99_ms": round(_pctl(ms, 99), 1),
                "max_ms": round(ms[-1], 1) if ms else 0,
                "throughput_rps": round(len(items) / span, 1) if span > 0.1 else None,
            }
        if reqs:
            span = max(r.t for r in reqs) - min(r.t for r in reqs)
            errors = sum(1 for r in reqs if not r.ok)
            out["_overall"] = {
                "count": len(reqs), "errors": errors,
                "error_rate_pct": round(100 * errors / len(reqs), 2),
                "wall_seconds": round(span, 1),
                "throughput_rps": round(len(reqs) / span, 1) if span > 0.1 else None,
            }
        else:
            out["_overall"] = {"count": 0, "errors": 0, "error_rate_pct": 0, "wall_seconds": 0, "throughput_rps": 0}
        return out


async def timed(recorder: LatencyRecorder, label: str, call: Callable[[], Awaitable[T]]) -> T:
    """Run one async HTTP/UI call, recording its latency and outcome regardless of how it ends."""
    t0 = time.monotonic()
    try:
        result = await call()
        ms = (time.monotonic() - t0) * 1000
        status = getattr(result, "status_code", 200)
        recorder.record(label, ms, status, status < 500)
        return result
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller to decide
        ms = (time.monotonic() - t0) * 1000
        recorder.record(label, ms, 0, False, error=f"{type(exc).__name__}: {exc}"[:200])
        raise


# --------------------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------------------


def render_report(report: dict[str, Any]) -> str:
    """A short human-readable summary for the pytest log (the full detail is saved as JSON)."""
    lines = [f"\n{'=' * 78}", f"LOAD TEST: {report['scenario']}", "=" * 78]
    cand = report["candidates"]
    lines.append(f"{cand['count']} candidates, {report['recruiters']['count']} recruiters, "
                 f"wall clock {report['wall_seconds']}s")
    lines.append("Scenarios: " + ", ".join(f"{k}={v}" for k, v in cand["by_scenario"].items()))
    srv = report["server"]
    lines.append(f"\nServer process (pid {report['server_pid']}, {srv['cpu_cores_visible']} cores visible):")
    lines.append(f"  CPU:    avg {srv['cpu_pct_of_machine_avg']}% / peak {srv['cpu_pct_of_machine_max']}% of the machine"
                 f"   (avg {srv['cpu_pct_of_one_core_avg']}% / peak {srv['cpu_pct_of_one_core_max']}% of one core)")
    lines.append(f"  Memory: {srv['rss_mb_start']} MB -> {srv['rss_mb_end']} MB  (peak {srv['rss_mb_max']} MB, "
                 f"growth {srv['rss_growth_mb']} MB)")
    lines.append(f"  Threads: started {srv['threads_start']}, peaked at {srv['threads_max']}")
    lines.append("\nHTTP / API calls:")
    http = report["http"]
    for label, s in http.items():
        if label == "_overall":
            continue
        lines.append(f"  {label:<22} n={s['count']:<5} errors={s['errors']:<3} "
                     f"p50={s['p50_ms']:>7}ms p95={s['p95_ms']:>7}ms p99={s['p99_ms']:>7}ms max={s['max_ms']:>8}ms "
                     f"throughput={s['throughput_rps']} req/s")
    o = http["_overall"]
    lines.append(f"  {'TOTAL':<22} n={o['count']:<5} errors={o['errors']:<3} error_rate={o['error_rate_pct']}% "
                 f"throughput={o['throughput_rps']} req/s")
    if report.get("ui"):
        rec = report["recruiters"]
        rate = rec["ui_errors"] / max(1, rec["iterations"])
        lines.append(f"\nRecruiter UI actions ({rec['count']} recruiters, {rec['iterations']} actions, "
                     f"{rec['ui_errors']} failed/timed out = {rate:.0%}):")
        for label, s in report["ui"].items():
            if label == "_overall":
                continue
            lines.append(f"  {label:<22} n={s['count']:<5} errors={s['errors']:<3} "
                         f"p50={s['p50_ms']:>7}ms p95={s['p95_ms']:>7}ms max={s['max_ms']:>8}ms")
    lines.append("\nData integrity: " + ("OK" if not report["data_integrity"]["problems"] else
                                         f"{len(report['data_integrity']['problems'])} problem(s)"))
    for p in report["data_integrity"]["problems"][:10]:
        lines.append(f"  - {p}")
    lines.append("=" * 78)
    return "\n".join(lines)


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
