"""Battle -> OutcomeBundle -> Uro: the Chronicler write path (TASK inc 2, brief section 3.3).

This module is the seam the whole experiment stresses. It:
- musters a contract's enemies as REAL Uro actors (a bundle may only reference existing actors,
  and an external game can never mint one through a bundle — so the game appends ActorCreated
  events itself through `append_beat`, the trusted-by-policy authored path; stress goal 7);
- derives the OutcomeBundle from the battle log (TASK B.6): participants = the self-attested
  scope root, witnesses = everyone who ended the fight ALIVE (survived or fled, either side,
  plus any seated town observer), casualties, feats, loot;
- reports it — `distill_outcome` in embed posture (the server posture POSTs the same JSON over
  HTTP; world/uro.py) — then runs `engine.react` so pack rules see the external deaths, and
  advances world time by hand (there is no game<->world time mapping; stress goal 2);
- reads back what Uro ACTUALLY recorded, because the bundle's word is not canon: protected
  casualties downgrade to rumors, out-of-cast refs drop, loot from the protected is refused.
  Pay is wired to the read-back, never to the local combat result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle, distill_outcome
from uro_core.domain.events import DomainEvent, actor_created, edge_added, item_created
from uro_core.pipeline.engine import Engine
from uro_core.timeline.models import Campaign

from ironwake import frictionlog
from ironwake.game.scenarios import Scenario
from ironwake.game.units import BattleReport
from ironwake.world.setup import VORLUND

# Loot the season seeds on enemies at muster: scenario unit name -> (item id, item name).
# Vorlund's blade is seeded at world genesis (he owns it before any contract names him).
SEEDED_LOOT: dict[str, tuple[str, str]] = {
    "Raider Osric": ("i:osrics-torc", "Osric's silver torc"),
    "Bone-Breaker Gurth": ("i:gurths-maul", "Gurth's iron maul"),
    "Standard-Bearer Krull": ("i:red-band-standard", "the Red Band standard"),
}


def enemy_ids_for(contract_id: str, scenario: Scenario) -> list[str]:
    """Deterministic Uro actor ids for a contract's enemies. Vorlund is THE seeded a:vorlund —
    the same protected canon figure every time he takes the field."""
    ids: list[str] = []
    for i, (cls, name) in enumerate(scenario.enemies):
        if cls == "Vorlund":
            ids.append(VORLUND)
            continue
        slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
        ids.append(f"a:rb-{contract_id}-{i}-{slug}")
    return ids


def muster_events(contract_id: str, scenario: Scenario, enemy_ids: list[str]) -> list[DomainEvent]:
    """ActorCreated (+faction/knows edges, +seeded loot) for a contract's fresh enemies.

    Committed via store.append_beat — NOTE (stress goal 7): nothing checks WHAT an embedding
    game appends here. The same call would accept an actor_died(a:vorlund). IRONWAKE only ever
    appends its own muster/lifecycle events, but it is trusted purely by policy — there is no
    append-time emitter whitelist.

    Every raider knows their captain: a routed (fled) enemy is a WITNESS, and this edge is how
    the company's legend travels to the Red Band itself (Vorlund hears what his survivors saw).
    """
    frictionlog.gap(
        gap="a commit-boundary check on WHO may append WHAT (emitter whitelist)",
        happened=(
            "the game hammers append_beat all season (enemy musters, contract threads, "
            "recruit edges) and nothing at the commit boundary ever asks whether an external "
            "caller should be allowed to — the SAME call would accept actor_died(a:vorlund), "
            "bypassing the ceiling distill_outcome enforces; IRONWAKE abstains by policy only"
        ),
        workaround="discipline: the game appends only its own muster/lifecycle event types",
        severity="major",
        needs=(
            "an append-time emitter whitelist (caused_by-keyed event-type allowlist for "
            "embedding consumers), so the Chronicler gate can't be side-stepped by its host"
        ),
        evidence="world/chronicle.py muster_events + cli/season.py append_beat call sites",
    )
    events: list[DomainEvent] = []
    for uid, (cls, name) in zip(enemy_ids, scenario.enemies, strict=True):
        if uid == VORLUND:
            continue  # seeded at genesis; never re-minted
        events.append(
            actor_created(actor_id=uid, name=name, tier=0, role=f"Red Band {cls.lower()}")
        )
        events.append(edge_added(src=uid, rel_type="member_of", dst="f:red-band"))
        events.append(edge_added(src=uid, rel_type="knows", dst=VORLUND))
        if name in SEEDED_LOOT:
            item_id, item_name = SEEDED_LOOT[name]
            events.append(item_created(item_id=item_id, name=item_name, owner_ref=uid, kind="loot"))
    return events


# --- deriving the bundle (TASK B.6) -----------------------------------------------------------


def derive_feats(
    report: BattleReport, scenario: Scenario, names: dict[str, str], site_name: str
) -> list[Feat]:
    """The battle log -> notable deeds, exactly per TASK B.6. Feat text is what witnesses will
    believe and taverns will retell — Uro records it verbatim as truth=unknown testimony."""
    feats: list[Feat] = []
    for uid, rounds in sorted(report.alone_rounds.items()):
        if rounds >= 2:
            feats.append(
                Feat(
                    actor=uid,
                    description=(
                        f"{names[uid]} of the Ironwake Company stood alone against the tide "
                        f"for {rounds} rounds at {site_name}"
                    ),
                )
            )
    for uid, kills in sorted(report.kills.items()):
        if kills >= 3:
            feats.append(
                Feat(
                    actor=uid,
                    description=f"{names[uid]} felled {kills} in the press at {site_name}",
                )
            )
    if scenario.feature:
        for uid in report.objective_holders:
            feats.append(
                Feat(actor=uid, description=f"{names[uid]} held {scenario.feature} to the last")
            )
    killer = report.killing_blows.get(VORLUND, "")
    if killer:
        feats.append(
            Feat(
                actor=killer,
                description=(
                    f"{names[killer]} cut down Captain Vorlund himself at {site_name} — "
                    f"the Red Captain fell, they swear it"
                ),
            )
        )
    return feats


def derive_loot(report: BattleReport, enemy_items: dict[str, str]) -> list[LootTransfer]:
    """One transfer per fallen enemy that owns a seeded item, to the merc credited with the
    kill (TASK B.6) — but only when the company WINS the field (you cannot strip the dead
    while routed), and only to a LIVING merc (the killer if they lived, else the first living
    merc). In a wipe nobody carries anything out — the loot is lost with the legend.

    The living-merc clamp exists because Uro does NOT make it: an early seed-7 run showed
    distill_outcome committing ItemTransferred to a merc who DIED in the same battle (to_ref
    liveness is never checked — chronicler.py validates existence/ownership of from_ref only),
    which let a massacred company get paid for a standard in a dead man's fist. Logged."""
    frictionlog.gap(
        gap="loot transfers land on someone who can actually carry the item off the field",
        happened=(
            "distill_outcome checks the item exists, from_ref owns it, and both refs are in "
            "participants — but never that to_ref survived: a bundle crediting loot to a "
            "same-battle CASUALTY commits fine (observed: the Red Band standard transferred "
            "to a merc who died in the same fight, and the purse paid out on a lost field)"
        ),
        workaround="the game clamps to_ref to a living merc and only loots a won field",
        severity="annoyance",
        needs="to_ref liveness (not-in-casualties) validation in distill_outcome's loot gate",
        evidence="world/chronicle.py derive_loot; uro_core/chronicler.py:166-182",
    )
    if report.outcome != "win":
        return []
    living_mercs = [
        u for u in report.survivors if u.startswith("a:merc-") and u not in report.casualties
    ]
    loot: list[LootTransfer] = []
    for enemy_uid, item_id in sorted(enemy_items.items()):
        if enemy_uid not in report.casualties:
            continue
        killer = report.killing_blows.get(enemy_uid, "")
        to_ref = killer if killer in living_mercs else (living_mercs[0] if living_mercs else "")
        if to_ref:
            loot.append(LootTransfer(item_id=item_id, from_ref=enemy_uid, to_ref=to_ref))
    return loot


def build_bundle(
    encounter_id: str,
    report: BattleReport,
    scenario: Scenario,
    names: dict[str, str],
    site_name: str,
    enemy_items: dict[str, str],
    observers: list[str] | None = None,
) -> OutcomeBundle:
    """The self-attested contract (stress goal 1): `participants` is every unit that started
    the battle — plus any town observer the game CHOOSES to seat. Uro cannot verify either
    claim; there is no parked-encounter registry. Witnesses = everyone who ended it alive."""
    participants = sorted(names) + sorted(observers or [])
    witnesses = sorted(report.survivors) + sorted(observers or [])
    return OutcomeBundle(
        encounter_id=encounter_id,
        participants=participants,
        witnesses=witnesses,
        casualties=list(report.casualties),
        feats=derive_feats(report, scenario, names, site_name),
        loot=derive_loot(report, enemy_items),
        duration_rounds=report.rounds,
    )


# --- reporting + the read-back (canon, not the game's word) ------------------------------------


@dataclass
class ReportOutcome:
    """What Uro actually recorded from a bundle — the read-back the game pays wages against."""

    commit_id: str
    committed_events: int
    canon_deaths: list[str] = field(default_factory=list)  # bundle casualties now status=dead
    downgraded: list[str] = field(default_factory=list)  # casualties that became mere rumors
    loot_landed: list[str] = field(default_factory=list)  # item ids that really moved
    loot_refused: list[str] = field(default_factory=list)


async def report_embed(
    store: PostgresEventStore,
    engine: Engine,
    campaign: Campaign,
    bundle: OutcomeBundle,
) -> tuple[str, int]:
    """Posture A write path: distill -> commit -> react. NOTE the react() call: the server's
    outcome endpoint runs it for you (uro_server.app.report_outcome), but the embed path does
    NOT — an embedding game that forgets this line silently loses every pack rule that should
    fire on external deaths (the war ratchet). Easy to miss; logged as a gap."""
    branch = campaign.branch_id
    events = await distill_outcome(store, branch, bundle)
    commit = await store.append_beat(branch, events)
    await engine.react(campaign, commit.commit_id, events)
    frictionlog.gap(
        gap="one Chronicler ingestion call for embedded games",
        happened=(
            "distill_outcome returns events but neither commits nor reacts; the embed caller "
            "must know to append_beat AND engine.react (the server endpoint does both) — "
            "forgetting react() silently kills every ActorDied-triggered pack rule"
        ),
        workaround="world/chronicle.py report_embed wraps the three calls",
        severity="annoyance",
        needs="a store/engine-level report_outcome(branch, bundle) that mirrors the server path",
        evidence="world/chronicle.py report_embed vs uro_server/app.py engine_deps.report_outcome",
    )
    return commit.commit_id, len(events)


async def read_back(
    store: PostgresEventStore,
    branch: str,
    bundle: OutcomeBundle,
    commit_id: str,
    committed: int,
) -> ReportOutcome:
    """Re-read canon after a report. The bundle said who died and what moved; only these reads
    say what the WORLD accepted. A tier-2 casualty (Vorlund) shows up here as `downgraded`:
    alive in canon, with a truth=unknown 'said to have fallen' claim standing in for the death."""
    out = ReportOutcome(commit_id=commit_id, committed_events=committed)
    for casualty in bundle.casualties:
        actor = await store.get_actor(branch, casualty)
        if actor is None:
            continue
        if actor.status == "dead":
            out.canon_deaths.append(casualty)
        else:
            claims = await store.claims_about(branch, casualty)
            if any(c.truth == "unknown" and "said to have fallen" in c.statement for c in claims):
                out.downgraded.append(casualty)
    for transfer in bundle.loot:
        owner_items = await store.items_owned_by(branch, transfer.to_ref)
        if transfer.item_id in owner_items:
            out.loot_landed.append(transfer.item_id)
        else:
            out.loot_refused.append(transfer.item_id)
    return out
