"""The shadow ledger — the numeric realm simulation Uro STRUCTURALLY cannot own.

This module is the wall, made code. Every House has strength / gold / influence / holdings /
ambition, every rival pair has accumulating tension, and each downtime tick runs income →
ambition → tension → war resolution as plain arithmetic. We tried to push each of these through
the declarative Reaction Layer first; the grammar has no counters, no arithmetic beyond compares,
no accumulating state, no loops, and no weighted tables — so the numbers live here, and EVERY
field kept here is a refusal-log entry (written at the bottom of this file, at the site of the
state the grammar refused).

Deterministic by construction: one seeded RNG (war dice only), pairs resolved in sorted order,
no wall-clock. Same seed → byte-identical realm history.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import frictionlog

# tuned constants (TASK.md §2.3)
WAR_THRESHOLD = 5
RECRUIT_COST = 10
UPKEEP = 1  # gold per point of strength per tick
WAR_DIE_SCALE = 2  # battle roll = strength + d6 * scale
HOLDING_VALUE: dict[str, int] = {
    "p:capital": 12,
    "p:saltport": 10,
    "p:silvermines": 12,
    "p:border-march": 6,
    "p:temple-district": 5,
    "p:oldkeep": 4,
    "p:greyfen": 3,
}


@dataclass
class House:
    house_id: str  # short name used in ledger printouts
    faction: str  # the Uro faction this shadow entry mirrors
    strength: int
    gold: int
    influence: int
    holdings: list[str]
    ambition: str  # expand | hoard | convert | ascend | survive
    loyalty: int  # to the Crown, 0..10


@dataclass
class Battle:
    attacker: str
    defender: str
    winner: str
    loser: str
    rolls: tuple[int, int]  # (attacker total, defender total)
    ceded_holding: str | None  # loser's lowest-value holding, if any


@dataclass
class TickReport:
    """What one downtime tick computed — the game reflects this into Uro qualitatively."""

    day: int
    income_lines: list[str] = field(default_factory=list)
    ambition_lines: list[str] = field(default_factory=list)
    wars_declared: list[tuple[str, str]] = field(default_factory=list)
    battles: list[Battle] = field(default_factory=list)
    distress_sales: list[tuple[str, str, str]] = field(default_factory=list)  # house, place, buyer
    landless: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class ShadowLedger:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.day = 0
        self.tension: dict[tuple[str, str], int] = {}
        self.at_war: set[tuple[str, str]] = set()
        # battle modifiers the Spymaster's intrigue buys (e.g. the forged raid order leaves the
        # ambushed side unready for the first clash) — house → one-battle strength penalty
        self.unready: dict[str, int] = {}
        self.houses: dict[str, House] = {
            h.house_id: h
            for h in [
                House("crown", "f:crown", 6, 30, 10, ["p:capital"], "survive", 10),
                House("vaelric", "f:vaelric", 8, 20, 6, ["p:border-march"], "expand", 4),
                House("corvane", "f:corvane", 5, 60, 5, ["p:saltport", "p:greyfen"], "hoard", 5),
                House("dellmoor", "f:dellmoor", 5, 1, 2, ["p:oldkeep"], "survive", 6),
                House("argent", "f:argent", 1, 120, 7, ["p:silvermines"], "ascend", 3),
                House("ashen", "f:ashen", 2, 15, 1, ["p:temple-district"], "convert", 0),
            ]
        }

    # --- intrigue hooks (called by the game after court beats) ---

    def add_tension(self, a: str, b: str, amount: int, why: str) -> int:
        p = _pair(a, b)
        self.tension[p] = self.tension.get(p, 0) + amount
        return self.tension[p]

    def reset_tension(self, a: str, b: str) -> None:
        self.tension[_pair(a, b)] = 0

    def make_peace(self, a: str, b: str) -> None:
        self.at_war.discard(_pair(a, b))
        self.reset_tension(a, b)

    def mark_unready(self, house: str, penalty: int) -> None:
        self.unready[house] = penalty

    # --- the downtime tick (TASK.md §2.3, exactly) ---

    def tick(self, days: int) -> TickReport:
        self.day += days
        rep = TickReport(day=self.day)
        for hid in sorted(self.houses):
            h = self.houses[hid]
            income = sum(HOLDING_VALUE[p] for p in h.holdings) - UPKEEP * h.strength
            h.gold += income
            rep.income_lines.append(f"{hid}: {'+' if income >= 0 else ''}{income} gold → {h.gold}")
        for hid in sorted(self.houses):
            h = self.houses[hid]
            if h.ambition == "expand" and h.gold >= RECRUIT_COST:
                recruits = h.gold // RECRUIT_COST
                h.strength += recruits
                h.gold -= recruits * RECRUIT_COST
                rep.ambition_lines.append(f"{hid} recruits +{recruits} strength (expand)")
            elif h.ambition == "ascend" and h.gold >= RECRUIT_COST:
                h.gold -= RECRUIT_COST
                h.influence += 1
                rep.ambition_lines.append(f"{hid} buys influence → {h.influence} (ascend)")
            elif h.ambition == "convert":
                h.influence += 1
                rep.ambition_lines.append(f"{hid} spreads the faith → influence {h.influence}")
            elif h.ambition == "survive" and h.gold < 0 and h.holdings:
                # distress sale: pawn the lowest-value holding to the Argent Ledger
                sold = min(h.holdings, key=lambda p: (HOLDING_VALUE[p], p))
                h.holdings.remove(sold)
                h.gold += HOLDING_VALUE[sold]
                buyer = self.houses["argent"]
                buyer.holdings.append(sold)
                buyer.gold -= HOLDING_VALUE[sold]
                rep.distress_sales.append((hid, sold, "argent"))
                rep.ambition_lines.append(f"{hid} pawns {sold} to argent (survive)")
        # tension → war (threshold crossing declares it)
        for p in sorted(self.tension):
            if self.tension[p] >= WAR_THRESHOLD and p not in self.at_war:
                self.at_war.add(p)
                rep.wars_declared.append(p)
        # war resolution: one battle per warring pair per tick
        for p in sorted(self.at_war):
            a, b = p
            ha, hb = self.houses[a], self.houses[b]
            roll_a = ha.strength - self.unready.pop(a, 0) + self.rng.randint(1, 6) * WAR_DIE_SCALE
            roll_b = hb.strength - self.unready.pop(b, 0) + self.rng.randint(1, 6) * WAR_DIE_SCALE
            if roll_a == roll_b:
                continue  # a bloody draw; the war grinds on
            win_id, lose_id = (a, b) if roll_a > roll_b else (b, a)
            winner, loser = self.houses[win_id], self.houses[lose_id]
            winner.strength = max(0, winner.strength - 1)
            loser.strength = max(0, loser.strength - 2)
            ceded: str | None = None
            if loser.holdings:
                ceded = min(loser.holdings, key=lambda pl: (HOLDING_VALUE[pl], pl))
                loser.holdings.remove(ceded)
                winner.holdings.append(ceded)
            rep.battles.append(
                Battle(
                    attacker=a,
                    defender=b,
                    winner=win_id,
                    loser=lose_id,
                    rolls=(roll_a, roll_b),
                    ceded_holding=ceded,
                )
            )
            if not loser.holdings:
                rep.landless.append(lose_id)
            if loser.strength <= 0:
                rep.broken.append(lose_id)
        return rep


# --- The refusal log for this module: every field above the grammar refused to own. ---
# These are the exact rules we WISHED we could ship in realm.py's RULE_PACK; each is written in
# the pack's own syntax plus the one primitive it lacks. (TASK.md §3 target 1 — the headline.)

frictionlog.gap(
    gap="The realm simulation (strength/gold/influence/tension per House) should be world "
    "state Uro owns, forkable and replayable like everything else",
    happened="the declarative Reaction Layer has no counters, no arithmetic beyond compares, "
    "no accumulating state, no loops, no weighted tables — the ENTIRE numeric realm lives in "
    "this module as ordinary Python, invisible to forks, exports, and rules",
    workaround="the shadow ledger (this file), reflected into Uro qualitatively each tick and "
    "manually snapshotted at fork points — see the 12-entry refusal log for the exact rules "
    "we could not write",
    severity="blocker",
    needs="the reserved engine-owned computation tier (D-33 Stage B, WASM) with numeric state "
    "that lives IN the event log",
    evidence="ledger.py (all of it); every refusal-log entry below",
)
frictionlog.refusal(
    name="Tension counter + threshold + reset",
    wished_rule="""{ "id": "tension-boils-to-war",
  "trigger": {"event": "ClaimRecorded", "where": {"origin": "hostile-intrigue"}},
  "then": [{"do": "increment_counter", "counter": "tension(f:vaelric,f:corvane)", "by": 1},
           {"do": "add_edge", "src": "f:vaelric", "rel": "at_war_with", "dst": "f:corvane",
            "if": "tension(f:vaelric,f:corvane) >= 5"}],
  "scope": {"faction": "f:court"} }
// and: a brokered marriage RESETS the pair's counter to 0""",
    missing="a per-pair accumulating counter + threshold trigger + reset — the grammar has no "
    "variables or state at all (conditions read projections, actions write a fixed union)",
    where="ledger.py ShadowLedger.tension / add_tension / tick step 3",
)
frictionlog.refusal(
    name="Economy: income minus upkeep, distress sale on deficit",
    wished_rule="""{ "id": "house-economy",
  "every_days": 20,
  "then": [{"do": "for_each", "faction": "*", "as": "H", "do": [
             {"do": "set", "var": "gold(H)",
              "expr": "gold(H) + sum(holding_value(p) for p owned_by H) - upkeep * strength(H)"},
             {"do": "transfer_holding", "if": "gold(H) < 0",
              "from": "H", "to": "f:argent", "pick": "lowest_value"}]}],
  "scope": {"faction": "f:court"} }""",
    missing="arithmetic (sum/multiply), accumulating gold state, a conditional loop over holdings"
    " and over factions",
    where="ledger.py ShadowLedger.tick steps 1-2 (income + distress sale)",
)
frictionlog.refusal(
    name="Comparative war trigger (cross-entity numeric compare)",
    wished_rule="""{ "id": "predator-smells-weakness",
  "every_days": 30,
  "when": {"kind": "compare", "left": "strength(f:vaelric)",
           "op": ">", "right": "strength(f:corvane) * 1.2"},
  "then": [{"do": "add_edge", "src": "f:vaelric", "rel": "at_war_with", "dst": "f:corvane"}],
  "scope": {"faction": "f:court"} }""",
    missing="cross-entity numeric comparison — `when` can only compare a fixed projection field "
    "(tier, world_day) to a constant; strength does not even exist engine-side",
    where="ledger.py ShadowLedger.tick step 4 (roll = strength + d6*scale)",
)
frictionlog.refusal(
    name="Weighted outcome table for a battle's aftermath",
    wished_rule="""{ "id": "fortunes-of-war",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "at_war_with"}},
  "then": [{"do": "roll_table", "weights": {"defection": 40, "siege": 30, "truce": 30},
            "outcomes": {
              "defection": [{"do": "remove_edge", "src": "a:captain-hurn",
                             "rel": "member_of", "dst": "f:vaelric"}],
              "siege":     [{"do": "set_thread_state", "thread": "t:border-war", "to": "active"}],
              "truce":     [{"do": "remove_edge", "src": "f:vaelric",
                             "rel": "at_war_with", "dst": "f:corvane"}]}}],
  "scope": {"faction": "f:court"} }""",
    missing="weighted RNG / outcome tables — the grammar is fully deterministic, and the engine "
    "offers no seeded-RNG surface to rules",
    where="ledger.py ShadowLedger.tick step 4 (rng.randint war dice)",
)
frictionlog.refusal(
    name="Fall of a House (count-to-zero + iterate members)",
    wished_rule="""{ "id": "fall-of-dellmoor",
  "trigger": {"event": "EdgeRemoved", "where": {"rel_type": "owns"}},
  "when": {"kind": "count", "what": "edges(src=f:dellmoor, rel=owns)", "op": "==", "value": 0},
  "then": [{"do": "set_thread_state", "thread": "t:dellmoor-decline", "to": "resolved"},
           {"do": "for_each", "member_of": "f:dellmoor", "as": "M",
            "do": [{"do": "remove_edge", "src": "M", "rel": "member_of", "dst": "f:dellmoor"}]}],
  "scope": {"faction": "f:dellmoor"} }""",
    missing="counting a projection set to zero + iteration over a faction's members",
    where="ledger.py ShadowLedger.tick (rep.landless / rep.broken)",
)
frictionlog.refusal(
    name="Recruitment (integer division, spend-to-buy loop)",
    wished_rule="""{ "id": "vaelric-raises-levies",
  "every_days": 20,
  "when": {"kind": "compare", "left": "gold(f:vaelric)", "op": ">=", "right": 10},
  "then": [{"do": "set", "var": "strength(f:vaelric)",
            "expr": "strength(f:vaelric) + gold(f:vaelric) // 10"},
           {"do": "set", "var": "gold(f:vaelric)", "expr": "gold(f:vaelric) % 10"}],
  "scope": {"faction": "f:vaelric"} }""",
    missing="integer arithmetic + mutable numeric state (strength/gold are not engine concepts)",
    where="ledger.py ShadowLedger.tick step 2 (ambition 'expand')",
)
frictionlog.refusal(
    name="Influence accumulation toward a coup threshold",
    wished_rule="""{ "id": "the-ledger-buys-the-throne",
  "every_days": 30,
  "then": [{"do": "increment_counter", "counter": "influence(f:argent)", "by": 1},
           {"do": "set_thread_state", "thread": "t:argent-debt", "to": "active",
            "if": "influence(f:argent) >= 12"}],
  "scope": {"faction": "f:argent"} }""",
    missing="an accumulating per-faction counter readable in a later condition",
    where="ledger.py House.influence / tick step 2 (ambition 'ascend')",
)
frictionlog.refusal(
    name="Rumor decay / expiry",
    wished_rule="""{ "id": "gossip-goes-stale",
  "every_days": 30,
  "then": [{"do": "expire_claims", "where": {"origin": "module"},
            "older_than_days": 60}],
  "scope": {"faction": "f:court"} }""",
    missing="temporal state on claims (age) + any action that retracts/decays a claim — the "
    "action union can only ADD claims; module rumors accumulate forever",
    where="sable_court.py stage 4 (rumor sets only ever grow across ticks)",
)
