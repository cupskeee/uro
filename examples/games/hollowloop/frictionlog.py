"""The running friction + refusal log — HOLLOWLOOP's scientific instrument.

Every entry is written AT THE CALL SITE, the moment the Uro API surprised, refused, downgraded,
forced a workaround, or was slower than expected (the brief: "keep a running friction log from
your very first commit ... do not reconstruct it from memory at the end"). GAP_REPORT.md is
assembled FROM this log, and the game prints it live at the end of a run so the receipts ship
with the output.

Three ledgers (the first two are the sibling games' shape, so reports stay comparable; the
third is HOLLOWLOOP-specific — this game's whole point is measured branching):
- GAPS     -> GAP_REPORT.md section 2 (the gap table).
- REFUSALS -> the Reaction-Layer refusal log: every rule the loop wanted that the declarative
              grammar could not express (the WASM-tier / D-33 Stage B evidence).
- TIMINGS  -> instrumented latencies (fork_branch, materialize-at-read, beat, time_skip),
              aggregated into the scale table that IS the branching-at-scale evidence.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

Severity = str  # blocker | major | annoyance | cosmetic


@dataclass(frozen=True)
class GapEntry:
    gap: str  # what we wanted
    happened: str  # actual API behavior / error / downgrade / measured cost
    workaround: str  # or "BLOCKED"
    severity: Severity
    needs: str  # the concrete engine change
    evidence: str  # call / file:line / timing number


@dataclass(frozen=True)
class RefusalEntry:
    name: str
    wished_rule: str  # the exact rule_pack rule, as we would write it if the grammar allowed
    missing: str  # the missing primitive (counter / arithmetic / vocabulary / event / ...)
    where: str  # the call site in the game that carries this logic instead


GAPS: list[GapEntry] = []
REFUSALS: list[RefusalEntry] = []
TIMINGS: dict[str, list[float]] = defaultdict(list)  # label -> durations in ms


def gap(
    *, gap: str, happened: str, workaround: str, severity: Severity, needs: str, evidence: str
) -> None:
    entry = GapEntry(
        gap=gap,
        happened=happened,
        workaround=workaround,
        severity=severity,
        needs=needs,
        evidence=evidence,
    )
    if entry not in GAPS:  # idempotent — a per-loop call site logs once per run
        GAPS.append(entry)


def refusal(*, name: str, wished_rule: str, missing: str, where: str) -> None:
    entry = RefusalEntry(name=name, wished_rule=wished_rule, missing=missing, where=where)
    if entry not in REFUSALS:
        REFUSALS.append(entry)


@contextmanager
def timed(label: str) -> Any:
    """Time an Uro call and file it under `label`. The scale run's whole evidence base."""
    start = time.perf_counter()
    try:
        yield
    finally:
        TIMINGS[label].append((time.perf_counter() - start) * 1000.0)


def timing_stats(label: str) -> dict[str, float]:
    samples = TIMINGS.get(label, [])
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": ordered[len(ordered) // 2],
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_ms": ordered[-1],
    }


def print_timings() -> None:
    if not TIMINGS:
        return
    print("\nINSTRUMENTED URO CALLS (ms):")
    print(f"  {'call':<28} {'n':>5} {'mean':>8} {'p50':>8} {'p95':>8} {'max':>8}")
    for label in sorted(TIMINGS):
        s = timing_stats(label)
        print(
            f"  {label:<28} {int(s['n']):>5} {s['mean_ms']:>8.1f} {s['p50_ms']:>8.1f} "
            f"{s['p95_ms']:>8.1f} {s['max_ms']:>8.1f}"
        )


def print_refusal_log() -> None:
    print(
        f"\nTHE REFUSAL LOG — {len(REFUSALS)} rules the declarative grammar could not express"
        " (refused outright, or — worse — accepted yet inert; each entry says which):"
    )
    for i, r in enumerate(REFUSALS, 1):
        print(f"\n  RL-{i} — {r.name}")
        for line in r.wished_rule.strip().splitlines():
            print(f"      {line}")
        print(f"    missing primitive: {r.missing}")
        print(f"    game code carries it instead at: {r.where}")


def print_gap_table() -> None:
    print(f"\nFRICTION LOG — {len(GAPS)} gaps hit at the API surface:")
    for i, g in enumerate(GAPS, 1):
        print(f"\n  G-{i} [{g.severity}] {g.gap}")
        print(f"      happened:   {g.happened}")
        print(f"      workaround: {g.workaround}")
        print(f"      needs:      {g.needs}")
        print(f"      evidence:   {g.evidence}")
