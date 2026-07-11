"""The running friction + refusal log — the game's scientific instrument.

Every entry is written AT THE CALL SITE, the moment the Uro API surprised, refused, downgraded,
or forced state into game code (the brief: "keep a running friction log from line one"). The
GAP_REPORT.md is assembled from THIS, not reconstructed from memory; the game prints the live
log at the end of every run so the receipts ship with the output.

Two ledgers:
- GAPS      → GAP_REPORT.md §2 (the gap table): wanted / happened / workaround / severity /
              engine change / evidence.
- REFUSALS  → GAP_REPORT.md §5 (the headline REFUSAL LOG): realm rules the declarative grammar
              could not express, each written as the exact `rule_pack` entry we wished we could
              write plus the missing primitive. This is the WASM-tier (D-33 Stage B) evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

Severity = str  # blocker | major | annoyance | cosmetic


@dataclass(frozen=True)
class GapEntry:
    gap: str  # what we wanted
    happened: str  # actual API behavior / error / downgrade
    workaround: str  # or "BLOCKED"
    severity: Severity
    needs: str  # the concrete engine change
    evidence: str  # call / file / line


@dataclass(frozen=True)
class RefusalEntry:
    name: str
    wished_rule: str  # the exact rule_pack rule, as we would write it if the grammar allowed
    missing: str  # the missing primitive (counter / arithmetic / loop / table / traversal / …)
    where: str  # the call site in the game that needed it


GAPS: list[GapEntry] = []
REFUSALS: list[RefusalEntry] = []


def gap(
    *, gap: str, happened: str, workaround: str, severity: Severity, needs: str, evidence: str
) -> None:
    GAPS.append(
        GapEntry(
            gap=gap,
            happened=happened,
            workaround=workaround,
            severity=severity,
            needs=needs,
            evidence=evidence,
        )
    )


def refusal(*, name: str, wished_rule: str, missing: str, where: str) -> None:
    REFUSALS.append(RefusalEntry(name=name, wished_rule=wished_rule, missing=missing, where=where))


def print_refusal_log() -> None:
    print(f"\nTHE REFUSAL LOG — {len(REFUSALS)} realm rules the declarative grammar refused:")
    for i, r in enumerate(REFUSALS, 1):
        print(f"\n  RL-{i} — {r.name}")
        for line in r.wished_rule.strip().splitlines():
            print(f"      {line}")
        print(f"    missing primitive: {r.missing}")
        print(f"    needed at: {r.where}")


def print_gap_table() -> None:
    print(f"\nFRICTION LOG — {len(GAPS)} gaps hit at the API surface:")
    for i, g in enumerate(GAPS, 1):
        print(f"\n  G-{i} [{g.severity}] {g.gap}")
        print(f"      happened:   {g.happened}")
        print(f"      workaround: {g.workaround}")
        print(f"      needs:      {g.needs}")
        print(f"      evidence:   {g.evidence}")
