"""The running friction + refusal log — THE SEVENTH VAULT's scientific instrument.

Every entry is written AT THE CALL SITE, the moment the Uro API surprised, refused, downgraded,
or forced state into game code (the brief: "keep a running friction log from the first commit").
GAP_REPORT.md is assembled from THIS, not reconstructed from memory; the arc prints the live log
at the end of every run so the receipts ship with the output.

Two ledgers (same instrument as the sibling games, so reports stay comparable):
- GAPS     -> GAP_REPORT.md section 2 (the gap table): wanted / happened / workaround / severity /
              engine change / evidence.
- REFUSALS -> the Reaction-Layer refusal log (stress goal S7): every rule the heist wanted but the
              declarative grammar could not express, written as the exact `rule_pack` entry we
              wished we could author plus the missing primitive. This is the WASM-tier
              (D-33 Stage B) evidence.
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
    missing: str  # the missing primitive (counter / arithmetic / vocabulary / event / ...)
    where: str  # the call site in the game that carries this logic instead


GAPS: list[GapEntry] = []
REFUSALS: list[RefusalEntry] = []


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
    if entry not in GAPS:  # idempotent — a per-contract call site logs once per run
        GAPS.append(entry)


def refusal(*, name: str, wished_rule: str, missing: str, where: str) -> None:
    entry = RefusalEntry(name=name, wished_rule=wished_rule, missing=missing, where=where)
    if entry not in REFUSALS:
        REFUSALS.append(entry)


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
