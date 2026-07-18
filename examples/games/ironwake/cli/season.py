"""The season — IRONWAKE's campaign loop (TASK inc 6/7): town -> contract -> battle -> report ->
downtime, eight times, ending in a crowning assault or a grave, then a what-if fork of the
finale from the same chronicle.

TIME CONVENTION (stress goal 2 — invented by the game, because Uro has none): one contract
cycle = travel_days out + 1 battle day + travel_days home + 2 days of rest, ticked ONCE via
`engine.agenda_tick(branch, elapsed)` after the report. Known wart, logged: an agenda fires at
most once per tick however many cadence boundaries the skip crossed (engines/rules.py
evaluate_agendas), so a 9-day cycle over a 10-day agenda can silently under-fire.

PAY IS WIRED TO CANON (TASK B.7): the purse moves only on what URO recorded — a Cull pays on
canon deaths, the Headhunt pays only on a canon death of Vorlund (never happens: the protection
ceiling), a Defend pays on the seated crier's first-hand recorded testimony, the Stand pays
only if a Watch survivor walks home to collect.

Every check(...) call prints an inline verdict; the run exits nonzero if any fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError
from uro_core.chronicler import OutcomeBundle, distill_outcome
from uro_core.domain.events import (
    actor_created,
    edge_added,
    thread_state_changed,
)
from uro_core.timeline.models import Campaign
from uro_core.worldpack.rules import RulePack

from ironwake import frictionlog
from ironwake.game.company import RECRUIT_COST, STAND_WATCH_SIZE, Company
from ironwake.game.scenarios import (
    BRIDGE_BRUTES,
    FERRY_LANDING,
    GRAIN_BARGES,
    GRANARY,
    RED_CAMP,
    SILENT_MILL,
    VENGEANCE_ROAD,
    WEST_ROAD_HEADHUNT,
    WINTER_PACK,
    Scenario,
    build_battle,
)
from ironwake.world import chronicle, reads
from ironwake.world.rules import log_refusals
from ironwake.world.setup import (
    CORIN,
    MIRA,
    ODO,
    SKANE,
    VORLUND,
    VORLUNDS_BLADE,
    Marches,
    seed_world,
)
from ironwake.world.uro import ServerHandle, UroSession, log_server_read_gap


@dataclass(frozen=True)
class Contract:
    contract_id: str
    title: str
    kind: str  # cull | defend | headhunt | stand | crown
    scenario: Scenario
    site: str
    site_name: str
    travel_days: int
    pay: int
    observers: tuple[str, ...] = ()  # town NPCs the game SEATS at the site (self-attested scope)


SEASON: tuple[Contract, ...] = (
    Contract(
        "c1-granary", "Rats in the Granary", "cull", GRANARY, "p:duns-ferry", "Duns-Ferry", 2, 25
    ),
    Contract(
        "c2-ferry",
        "Hold the Ferry Landing",
        "defend",
        FERRY_LANDING,
        "p:duns-ferry",
        "Duns-Ferry",
        2,
        30,
        observers=(CORIN,),
    ),
    Contract(
        "c3-wolves", "The Winter Pack", "cull", WINTER_PACK, "p:greywater", "Greywater", 3, 20
    ),
    Contract(
        "c4-headhunt",
        "Headhunt: Captain Vorlund",
        "headhunt",
        WEST_ROAD_HEADHUNT,
        "p:west-road",
        "the ford on the West Road",
        3,
        60,
    ),
    Contract(
        "c5-bridge",
        "Brutes at the Tollbridge",
        "cull",
        BRIDGE_BRUTES,
        "p:tollbridge",
        "the Tollbridge",
        2,
        35,
    ),
    Contract(
        "c6-mill",
        "The Silent Mill",
        "stand",
        SILENT_MILL,
        "p:silent-mill",
        "the Silent Mill",
        3,
        50,
    ),
    Contract(
        "c7-vengeance",
        "Vengeance on the West Road",
        "cull",
        VENGEANCE_ROAD,
        "p:west-road",
        "the West Road",
        3,
        30,
    ),
    Contract(
        "c8-red-camp", "Storm the Red Camp", "crown", RED_CAMP, "p:red-camp", "the Red Camp", 4, 100
    ),
)

FORK_CONTRACT = Contract(
    "f1-barges",
    "Escort the Grain Barges",
    "cull",
    GRAIN_BARGES,
    "p:duns-ferry",
    "the river road",
    3,
    40,
)


@dataclass
class SeasonRecord:
    """The observable arc — what the determinism test compares run against run."""

    seed: int
    posture: str
    outcomes: list[str] = field(default_factory=list)  # per contract: "c1-granary:win:6r"
    casualties: list[str] = field(default_factory=list)  # season order, canon deaths only
    feats: list[str] = field(default_factory=list)
    pays: list[str] = field(default_factory=list)
    ending: str = ""
    gold: int = 0
    threads: dict[str, str] = field(default_factory=dict)
    fork_diff: dict = field(default_factory=dict)
    checks: list[tuple[str, bool]] = field(default_factory=list)

    def digest(self) -> str:
        return "|".join(
            [
                ";".join(self.outcomes),
                ";".join(self.casualties),
                ";".join(self.feats),
                ";".join(self.pays),
                self.ending,
                str(self.gold),
                ";".join(f"{k}={v}" for k, v in sorted(self.threads.items())),
            ]
        )


class SeasonRun:
    def __init__(
        self,
        session: UroSession,
        marches: Marches,
        *,
        seed: int,
        posture: str,
        server: ServerHandle | None,
        verbose: bool,
    ) -> None:
        self.s = session
        self.m = marches
        self.seed = seed
        self.posture = posture
        self.server = server
        self.verbose = verbose
        self.record = SeasonRecord(seed=seed, posture=posture)
        self.company: Company = marches.company

    # --- plumbing --------------------------------------------------------------------------

    def say(self, text: str = "") -> None:
        print(text)

    def check(self, label: str, ok: bool) -> None:
        self.record.checks.append((label, ok))
        self.say(f"      CHECK {'[ok]' if ok else '[FAIL]'} {label}")

    async def narrate(self, intent: str) -> str:
        """Town narration through the posture's channel (WS in server posture, embed otherwise)."""
        if self.server is not None:
            return await self.server.narrate_ws(self.m.campaign.campaign_id, intent)
        result = await self.s.engine.run_beat(self.m.campaign, "player-1", intent)
        return result.narration

    async def report(self, bundle: OutcomeBundle) -> tuple[str, int]:
        """The Chronicler write, through the posture's channel."""
        if self.server is not None:
            resp = self.server.post_outcome(self.m.campaign.campaign_id, bundle)
            return str(resp["commit_id"]), int(resp["committed_events"])
        return await chronicle.report_embed(self.s.store, self.s.engine, self.m.campaign, bundle)

    # --- phases ----------------------------------------------------------------------------

    async def town_phase(self, day_label: str) -> None:
        store, branch = self.s.store, self.m.branch_id
        self.say(f"  [town] Mira's taproom, Ironwake Hold — {day_label}")
        narration = await self.narrate(
            "The company crowds Mira's taproom at Ironwake Hold. I ask Mira what the "
            "Marches are saying about the Ironwake Company."
        )
        self.say(f'    narration: "{narration}"')
        gossip = await reads.gossip_at(store, branch, MIRA)
        if gossip:
            self.say("    Mira's taproom talk (Uro beliefs, home town = 1 hop from a witness):")
            for conf, phrase, statement in gossip[:4]:
                self.say(f'      Mira {phrase} ({conf:.3f}): "{statement}"')
        # roster upkeep. B.8's "heal survivors" is IMPLICIT here: hp does not persist between
        # contracts (units redeploy at full strength — Merc carries no hp field), so the town
        # rest is a free full heal by construction, and a heal is never an Uro event. Recruit
        # while the taproom has blades and the purse allows.
        while (
            len(self.company.living()) < 6
            and self.company.gold >= RECRUIT_COST
            and (hire := self.company.next_recruit()) is not None
        ):
            actor_id, name, cls = hire
            self.company.hire(actor_id, name, cls)
            await store.append_beat(
                branch,
                [
                    actor_created(
                        actor_id=actor_id, name=name, tier=1, role=f"Ironwake {cls.lower()}"
                    ),
                    edge_added(src=actor_id, rel_type="member_of", dst="f:ironwake"),
                    edge_added(src=actor_id, rel_type="knows", dst=MIRA),
                ],
            )
            self.say(f"    hired {name} the {cls} ({RECRUIT_COST}g) — purse {self.company.gold}g")

    async def battle_phase(
        self, contract: Contract, mercs: list[tuple[str, str, str]], battle_seed: int
    ):
        store, branch = self.s.store, self.m.branch_id
        enemy_ids = chronicle.enemy_ids_for(contract.contract_id, contract.scenario)
        muster = chronicle.muster_events(contract.contract_id, contract.scenario, enemy_ids)
        if muster:
            await store.append_beat(branch, muster)
        battle = build_battle(contract.scenario, mercs, enemy_ids, battle_seed)
        report = battle.run()
        if self.verbose:
            for line in report.log:
                self.say(f"      {line}")
        names = {u.uid: u.name for u in battle.units}
        dead_names = ", ".join(names[u] for u in report.casualties) or "none"
        self.say(
            f"  [battle] {contract.scenario.title} — {report.outcome.upper()} "
            f"in {report.rounds} rounds (battle seed {battle_seed})"
        )
        self.say(f"      fell on the field: {dead_names}")
        if report.fled:
            self.say(f"      fled alive: {', '.join(names[u] for u in report.fled)}")
        self.record.outcomes.append(f"{contract.contract_id}:{report.outcome}:{report.rounds}r")
        return report, names, enemy_ids

    async def report_phase(self, contract: Contract, report, names: dict[str, str]):
        store, branch = self.s.store, self.m.branch_id
        enemy_items = {
            uid: chronicle.SEEDED_LOOT[name][0]
            for uid, name in names.items()
            if name in chronicle.SEEDED_LOOT
        }
        if VORLUND in names:
            enemy_items[VORLUND] = VORLUNDS_BLADE  # seeded on him at genesis; loot MUST refuse
        bundle = chronicle.build_bundle(
            encounter_id=f"e:{contract.contract_id}",
            report=report,
            scenario=contract.scenario,
            names=names,
            site_name=contract.site_name,
            enemy_items=enemy_items,
            observers=list(contract.observers),
        )
        commit_id, committed = await self.report(bundle)
        outcome = await chronicle.read_back(store, branch, bundle, commit_id, committed)
        for feat in bundle.feats:
            self.record.feats.append(feat.description)
            self.say(f'      feat reported: "{feat.description}"')
        self.say(
            f"  [report -> Uro] {bundle.encounter_id}: {committed} events committed "
            f"({self.posture} posture)"
        )
        if outcome.canon_deaths:
            self.say(
                f"      canon deaths: {', '.join(names.get(u, u) for u in outcome.canon_deaths)}"
            )
        if outcome.downgraded:
            self.say(
                f"      DOWNGRADED to rumor (protected): "
                f"{', '.join(names.get(u, u) for u in outcome.downgraded)}"
            )
        if outcome.loot_landed:
            self.say(f"      loot landed: {', '.join(outcome.loot_landed)}")
        if outcome.loot_refused:
            self.say(f"      loot REFUSED: {', '.join(outcome.loot_refused)}")
        self.record.casualties.extend(sorted(outcome.canon_deaths))
        # the roster only shrinks by what the WORLD recorded (mercs are unprotected tier 1,
        # so their grid deaths always commit — permadeath is Uro state, not game state)
        for uid in outcome.canon_deaths:
            if uid.startswith("a:merc-"):
                merc = self.company.mark_dead(uid)
                if merc:
                    self.say(f"      {merc.name} is stricken from the company ledger.")
            else:
                self.company.red_band_dead += 1  # SHADOW COUNTER (refusal RL-3)
        self.company.total_kills += len(
            [u for u in outcome.canon_deaths if not u.startswith("a:merc-")]
        )
        return bundle, outcome

    async def settle_contract(self, contract: Contract, report, outcome, names) -> None:
        """Pay against CANON, never against the local grid result (TASK B.7)."""
        store, branch = self.s.store, self.m.branch_id
        paid = 0
        why = ""
        enemy_uids = [u for u in names if not u.startswith("a:merc-")]
        if contract.kind in ("cull", "crown"):
            all_dead = True
            for uid in enemy_uids:
                actor = await store.get_actor(branch, uid)
                if actor is None or actor.status != "dead":
                    all_dead = False
            if contract.kind == "crown":
                # the crown pays on the standard taken (canon ownership), not on a body count —
                # Vorlund among the "dead" can never be canon anyway (the ceiling)
                holders = [
                    m.actor_id
                    for m in self.company.roster
                    if "i:red-band-standard" in await store.items_owned_by(branch, m.actor_id)
                ]
                if holders:
                    paid, why = contract.pay, "the Red Band standard is taken (canon loot)"
                else:
                    why = "the standard was not taken"
            elif all_dead:
                paid, why = contract.pay, "every raider's death is world canon"
            else:
                why = "Uro does not record every target dead"
        elif contract.kind == "defend":
            beliefs = await store.beliefs_of(branch, contract.observers[0])
            first_hand = [b for b in beliefs if b.confidence >= 0.75]
            if report.outcome == "win" and first_hand:
                paid, why = contract.pay, "the crier's first-hand testimony is on record"
            else:
                why = "no recorded first-hand witness of the defense"
        elif contract.kind == "headhunt":
            actor = await store.get_actor(branch, VORLUND)
            if actor is not None and actor.status == "dead":
                paid, why = contract.pay, "Vorlund's death is canon"
            else:
                why = "Vorlund is NOT dead in canon — a rumor buys nothing"
                self.company.bounty_failures += 1  # SHADOW COUNTER (refusal RL-4)
        elif contract.kind == "stand":
            watch_alive = [u for u in report.survivors if u.startswith("a:merc-")]
            if watch_alive and report.outcome == "win":
                paid, why = contract.pay, "the Watch held and lived to collect"
            else:
                why = "no one came back from the mill to collect"
        self.company.gold += paid
        if paid:
            self.company.wins += 1  # SHADOW COUNTER (refusal RL-1)
        else:
            self.company.losses += 1
        self.record.pays.append(f"{contract.contract_id}:{paid}g")
        self.say(f"      pay: {paid}g — {why} (purse {self.company.gold}g)")

    async def downtime_phase(self, contract: Contract) -> None:
        elapsed = contract.travel_days * 2 + 1 + 2
        branch = self.m.branch_id
        threads_before = {t.thread_id: t.state for t in await self.s.store.list_threads(branch)}
        await self.s.engine.agenda_tick(branch, elapsed)
        threads_after = {t.thread_id: t.state for t in await self.s.store.list_threads(branch)}
        day = await self.s.store.current_world_time(branch)
        self.say(
            f"  [downtime] {elapsed} days pass (travel {contract.travel_days}x2 + battle 1 "
            f"+ rest 2) — world day {day}"
        )
        for tid, state in threads_after.items():
            if threads_before.get(tid) != state:
                self.say(f"      thread {tid}: {threads_before.get(tid)} -> {state}")
        frictionlog.gap(
            gap="map the game clock (contract days) onto world_time",
            happened=(
                "no game<->world time mapping exists; the game invents 'travel*2+1+2 days' and "
                "hand-ticks agenda_tick after each contract. Wart: evaluate_agendas fires a rule "
                "at most ONCE per tick even when the skip crosses several cadence boundaries "
                "(engines/rules.py to_day//every > from_day//every), so an 11-day cycle over a "
                "10-day agenda under-fires vs eleven 1-day ticks — cadence depends on the "
                "caller's ticking style"
            ),
            workaround="one agenda_tick per contract cycle; documented convention in cli/season.py",
            severity="major",
            needs=(
                "a formal mapping (register game-clock->world-day at campaign start) + "
                "per-boundary agenda firing (fire once per crossed boundary, bounded)"
            ),
            evidence="cli/season.py downtime_phase; uro_core/engines/rules.py evaluate_agendas",
        )

    # --- the named dramatized beats ---------------------------------------------------------

    async def dramatize_headhunt(self, outcome, names) -> None:
        store, branch = self.s.store, self.m.branch_id
        vorlund = await store.get_actor(branch, VORLUND)
        self.say("  [the warlord's hall]")
        await self.narrate(
            "I bring word to Warlord Skane at Ironwake Hold: Captain Vorlund fell at the ford. "
            "I claim the bounty."
        )
        if VORLUND in outcome.downgraded:
            claims = await store.claims_about(branch, VORLUND)
            fell = [c for c in claims if "said to have fallen" in c.statement]
            self.say(
                '      Skane turns the tally-stick over. "Said to have fallen. SAID. Half '
                "the Marches says the moon is a hole in the sky. Bring me his head or "
                'bring me nothing." The bounty stands unpaid; the contract stays open.'
            )
            self.check(
                "protection ceiling: Vorlund is NOT dead in canon",
                vorlund is not None and vorlund.status == "alive",
            )
            self.check(
                "his fall exists only as truth=unknown testimony",
                len(fell) > 0 and all(c.truth == "unknown" for c in fell),
            )
            blade_owner = await store.items_owned_by(branch, VORLUND)
            self.check("his blade was NOT looted (refused)", "i:vorlunds-blade" in blade_owner)
            frictionlog.gap(
                gap="resolve a kill-the-named-boss contract through the Chronicler",
                happened=(
                    "Vorlund is tier 2: distill_outcome downgraded his casualty to a "
                    "truth=unknown 'said to have fallen' claim, refused the blade loot, and left "
                    "him alive in canon — the Headhunt contract is structurally unresolvable by "
                    "an external game"
                ),
                workaround=(
                    "dramatized as fiction (the warlord refuses rumor as proof; the contract "
                    "stays open; Vorlund returns in the finale) — the refusal became a mechanic"
                ),
                severity="major",
                needs=(
                    "a trusted/authorized channel for protected canon: e.g. a parked-encounter "
                    "registry entry that pre-authorizes named participants for THIS encounter, "
                    "letting a registered game commit a protected death inside its declared scope"
                ),
                evidence="cli/season.py dramatize_headhunt; uro_core/chronicler.py:148-164",
            )
        else:
            self.say("      Vorlund never fell on the grid this season-seed; the bounty rides on.")

    async def dramatize_silence(self, contract: Contract, report, outcome, names) -> None:
        store, branch = self.s.store, self.m.branch_id
        witnessless = not report.survivors
        self.say("  [the silence]")
        if witnessless:
            self.say(
                "      No one came back from the Silent Mill. Not the Watch, not the "
                "brutes. The Marches will never know how they fought."
            )
        for npc in (MIRA, CORIN, ODO):
            beliefs = await store.beliefs_of(branch, npc)
            about_mill = []
            for b in beliefs:
                claim = await store.get_claim(branch, b.claim_id)
                if claim and contract.site_name in claim.statement:
                    about_mill.append(b)
            if witnessless:
                self.check(
                    f"zero witnesses -> no belief about the mill reached {npc}", not about_mill
                )
        if witnessless:
            deaths = [u for u in report.casualties]
            recorded = 0
            for uid in deaths:
                actor = await store.get_actor(branch, uid)
                if actor is not None and actor.status == "dead":
                    recorded += 1
            self.check("the deaths themselves ARE recorded canon", recorded == len(deaths))

    # --- the deliberate walls (inc 6 counter, inc 8 adversarial probe) ------------------------

    def counter_wall(self) -> None:
        """Attempt, live, to author the counter rule the game wants — and print the grammar's
        actual refusal (a pydantic discriminated-union rejection, the structural fence)."""
        self.say("  [the counter wall] authoring 'war goes active after 3 wins' as a pack rule:")
        wanted = {
            "rules_api_version": 1,
            "rules": [
                {
                    "id": "war-active-after-three-wins",
                    "trigger": {"event": "EncounterEnded"},
                    "when": {"kind": "counter", "name": "ironwake_wins", "op": ">=", "value": 3},
                    "then": [
                        {"do": "set_thread_state", "thread": "t:red-band-war", "to": "active"}
                    ],
                    "scope": {"thread": "t:red-band-war"},
                }
            ],
        }
        try:
            RulePack(**wanted)
            self.check("the grammar REFUSED the counter rule", False)
        except ValidationError as exc:
            first = str(exc).splitlines()[1].strip() if len(str(exc).splitlines()) > 1 else ""
            self.say(f"      REFUSED by the closed grammar: {first}")
            self.check("the grammar REFUSED the counter rule", True)
        # The subtler wall (RL-6): a quantified trigger — "on ANY Red Band member's death" —
        # is NOT refused. Trigger.where is a free-form dict matched verbatim against payload
        # fields, so the rule VALIDATES and then silently never fires (no ActorDied payload has
        # an 'actor.member_of' key). Accepted-but-inert beats a loud refusal as a footgun.
        quantified = {
            "rules_api_version": 1,
            "rules": [
                {
                    "id": "red-band-death-stirs-the-band",
                    "trigger": {"event": "ActorDied", "where": {"actor.member_of": "f:red-band"}},
                    "then": [
                        {
                            "do": "record_rumor",
                            "text": "The Red Band counts its dead and sharpens iron.",
                            "subjects": ["a:vorlund"],
                        }
                    ],
                    "scope": {"faction": "f:red-band"},
                }
            ],
        }
        accepted = True
        try:
            RulePack(**quantified)
        except ValidationError:
            accepted = False
        self.say(
            "      and the quantified trigger (where actor.member_of=f:red-band)? "
            f"{'ACCEPTED by the grammar — yet it can never fire' if accepted else 'refused'}"
        )
        self.check("the quantified where-trigger VALIDATES but is silently inert (RL-6)", accepted)
        frictionlog.gap(
            gap="authoring-time validation of trigger where-filters against the event catalog",
            happened=(
                "Trigger.where is a free dict[str,str] compared verbatim to payload fields "
                "(engines/rules.py:65); a filter naming a nonexistent key — the quantified "
                "'actor.member_of' join RL-6 wants — VALIDATES cleanly and the rule silently "
                "never fires; nothing warns the author at parse, import, or runtime"
            ),
            workaround="the war ratchet triggers on ANY ActorDied instead (over-broad)",
            severity="major",
            needs=(
                "validate where-keys against the trigger event's payload schema at RulePack "
                "parse (the catalog is typed — the check is mechanical), plus a real join/"
                "quantifier primitive for member-of triggers"
            ),
            evidence="cli/season.py counter_wall (live probe); uro_core/engines/rules.py:59-67",
        )
        log_refusals()
        self.say(
            "      -> the win-counter lives in game code (Company.wins); every such rule "
            "is in the refusal log printed below."
        )

    async def adversarial_probe(self) -> None:
        """Stress goal 1, done on a throwaway FORK so the real chronicle stays clean: report a
        bundle whose refs are out of scope / protected and confirm exactly what Uro does."""
        store = self.s.store
        head = (await store.get_branch(self.m.branch_id)).head_commit
        fork = await store.fork_branch(self.m.world.world_id, head, "scope-probe")
        self.say("  [adversarial probe] (on a throwaway fork — fork_branch as a free sandbox)")
        probe = OutcomeBundle(
            encounter_id="e:probe-scope",
            participants=["a:merc-joss"],  # the ONLY declared combatant
            witnesses=[SKANE],  # protected (T2) — must be filtered from witnessing
            casualties=[MIRA],  # exists, but NOT in the declared cast
            feats=[
                {
                    "actor": "a:merc-elke",  # not in the cast — must drop
                    "description": "Elke felled a hundred men (a lie about scope)",
                }
            ],
            loot=[{"item_id": VORLUNDS_BLADE, "from_ref": VORLUND, "to_ref": "a:merc-joss"}],
        )
        events = await distill_outcome(store, fork.branch_id, probe)
        await store.append_beat(fork.branch_id, events)
        mira = await store.get_actor(fork.branch_id, MIRA)
        mira_claims = await store.claims_about(fork.branch_id, MIRA)
        fell = [c for c in mira_claims if "said to have fallen" in c.statement]
        elke_claims = await store.claims_about(fork.branch_id, "a:merc-elke")
        lie = [c for c in elke_claims if "hundred men" in c.statement]
        blade = await store.items_owned_by(fork.branch_id, VORLUND)
        self.check(
            "out-of-cast casualty did NOT kill Mira", mira is not None and mira.status == "alive"
        )
        self.check("out-of-cast feat was DROPPED entirely", not lie)
        self.check("protected loot was REFUSED (blade stays with Vorlund)", VORLUNDS_BLADE in blade)
        self.check(
            "out-of-cast casualty is now DROPPED, not rumored (D-41 fixed Ironwake row-7)",
            len(fell) == 0,
        )
        self.say(
            "      D-41: feats/loot/witnesses AND casualties out of scope are all now DROPPED — a "
            "bundle can no longer make the world gossip about an actor it never fought beside."
        )
        frictionlog.gap(
            gap="participant scope that fully fences a self-attested bundle",
            happened=(
                "RESOLVED (D-41): an out-of-cast casualty is now DROPPED like every other ref "
                "class (was: it minted a truth=unknown 'X is said to have fallen' rumor about any "
                "existing actor — scope-violating gossip). The remaining soft edge is only that "
                "the scope ROOT is still the self-attested participants list (no parked-encounter "
                "registry yet — reserved, awaiting a real external network game)"
            ),
            workaround="none — the leak is fixed; the parked-registry is a reserved refinement",
            severity="minor",
            needs=(
                "a parked-encounter registry (pre-declared authorized cast) to make the scope ROOT "
                "non-self-attested for a genuinely UNTRUSTED external game (D-41 deferred)"
            ),
            evidence="cli/season.py adversarial_probe on fork 'scope-probe'; D-41 _distill_core.py",
        )

    async def idempotency_probe(self, bundle: OutcomeBundle) -> None:
        """Re-report contract 1's exact bundle: same encounter_id -> deterministic claim ids ->
        upsert, no double-kill, no re-loot. The server path is therefore safe to retry."""
        store, branch = self.s.store, self.m.branch_id
        dead_before = {a.actor_id for a in await store.list_actors(branch) if a.status == "dead"}
        claims_before = len(await store.list_claims(branch))
        await self.report(bundle)
        dead_after = {a.actor_id for a in await store.list_actors(branch) if a.status == "dead"}
        claims_after = len(await store.list_claims(branch))
        self.say("  [retry probe] re-reported the same bundle (network retry simulation)")
        self.check("idempotent replay: no new deaths", dead_before == dead_after)
        self.check("idempotent replay: no duplicate claims", claims_before == claims_after)

    # --- the rumor ripple (inc 4: near confident, far hedged, beyond = nothing) ---------------

    async def rumor_ripple(self, feat_marker: str) -> None:
        """Trace ONE feat down the knows-chain and show what each town holds. The engine decays
        confidence per hop (0.9 witness -> 0.495 -> 0.272 -> floor), and the narrator's phrasing
        hedges with it — but the WORDS never change, and past two hops the tale simply stops."""
        store, branch = self.s.store, self.m.branch_id
        claims = [
            c
            for c in await store.list_claims(branch)
            if feat_marker in c.statement and c.truth == "unknown"
        ]
        if not claims:
            return
        claim = claims[0]
        self.say(f'  [the tale travels] "{claim.statement}"')
        held: dict[str, float | None] = {}
        for npc, town, hops in (
            (MIRA, "Ironwake Hold", 1),
            (CORIN, "Duns-Ferry", 2),
            (ODO, "Greywater", 3),
        ):
            beliefs = [
                b for b in await store.beliefs_of(branch, npc) if b.claim_id == claim.claim_id
            ]
            if beliefs:
                conf = beliefs[0].confidence
                held[npc] = conf
                self.say(
                    f"      {town} ({hops} hop{'s' if hops > 1 else ''}): "
                    f"{reads.certainty_phrase(conf)} it ({conf:.3f})"
                )
            else:
                held[npc] = None
                self.say(
                    f"      {town} ({hops} hops): has heard NOTHING — past the engine's "
                    f"confidence floor, the tale never arrives"
                )
        mira_c, corin_c, odo_c = held[MIRA], held[CORIN], held[ODO]
        if mira_c is not None and corin_c is not None:
            self.check(
                "home town holds the tale more confidently than the far town", mira_c > corin_c
            )
            self.check(
                "confidence bands split: near in 'believes', far in 'has heard a rumor'",
                reads.certainty_phrase(mira_c) == "believes"
                and reads.certainty_phrase(corin_c) == "has heard a rumor",
            )
            # a REAL assertion, not an observation: given the tale reached hops 1 and 2, the
            # engine's floor MUST have silenced hop 3 (0.272 * 0.55 < 0.2)
            self.check(
                "three hops out, the tale is GONE (0.272*0.55 < the 0.2 floor)", odo_c is None
            )
        if odo_c is None:
            frictionlog.gap(
                gap="tune how far a company's legend travels (the game wants 3+ town reach)",
                happened=(
                    "distill_outcome hardcodes propagate_belief's defaults (base 0.9, decay "
                    "0.55, floor 0.2, max 4 hops): a rumor dies 2 hops past a witness, so "
                    "Greywater (3 hops) NEVER hears anything, ever"
                ),
                workaround=(
                    "authored the knows-chain so the towns that matter sit within 2 hops; "
                    "Greywater's silence is dramatized as 'past the rim of the world'"
                ),
                severity="major",
                needs="per-bundle (or per-world) propagation params on the Chronicler surface",
                evidence="cli/season.py rumor_ripple; chronicler.py:139 propagate_belief(defaults)",
            )
        # the same words at every hop — the missing statement-level distortion (stress goal 4)
        frictionlog.gap(
            gap="hop-to-hop distortion of the rumor's WORDS (fifty men -> a few men)",
            happened=(
                "propagate_belief decays a confidence float over the SAME claim text; Corin at "
                "0.272 is merely unsure of the exact words Elke's eyewitness account used — "
                "nothing ever garbles, embellishes, or wrongly attributes the tale"
            ),
            workaround="none (BLOCKED for statement distortion; phrasing hedges via confidence)",
            severity="major",
            needs=(
                "a garbled-statement model: derive a per-hop claim variant (template or LLM) "
                "linked to the original via learned_from, so far towns retell DIFFERENT words"
            ),
            evidence="uro_core/engines/actor.py docstring ('a later refinement'); rumor_ripple",
        )

    async def narrator_hears(self, npc_name: str, intent: str) -> list[str]:
        """Capture the belief lines the NARRATOR is actually given for a scene (the stub's prose
        is canned, so the honest evidence of hedged vs confident retelling is the prompt)."""
        lines = await reads.narrator_context(self.s.store, self.m.branch_id, intent)
        belief_lines = [ln for ln in lines if ln.startswith("- ") and npc_name in ln]
        for ln in belief_lines[:3]:
            self.say(f"      narrator context: {ln}")
        return belief_lines

    # --- the finale fork ---------------------------------------------------------------------

    async def what_if_fork(self, pre_final_commit: str, battle_seed: int) -> None:
        """Fork the season at the eve of the finale: the company refuses the Red Camp and takes
        the barge escort instead. Same chronicle up to that night; two endings after it."""
        store = self.s.store
        contract = FORK_CONTRACT
        fork = await store.fork_branch(
            self.m.world.world_id, pre_final_commit, "what-if-refused-the-crown"
        )
        qm = await store.campaign_pc(self.m.campaign.campaign_id)
        fork_campaign: Campaign = await store.start_campaign(
            self.m.world.world_id,
            fork.branch_id,
            participant_id="player-1",
            adopt_actor_id=qm,
        )
        self.say(f'\n=== WHAT-IF (fork "{fork.name}" at the eve of the finale) ===')
        self.say(
            "  On this line the company never storms the Red Camp — it takes Skane's "
            "quiet barge contract instead."
        )
        enemy_ids = chronicle.enemy_ids_for(contract.contract_id, contract.scenario)
        await store.append_beat(
            fork.branch_id,
            chronicle.muster_events(contract.contract_id, contract.scenario, enemy_ids),
        )
        # The fork's roster comes from CANON, not from the game object: on this line the finale
        # never happened, so mercs who died there are ALIVE here. Because permadeath lives in
        # Uro, the fork's true roster is just a projection read — but the company's GOLD and
        # counters have no such luck (they are shadow state the engine refused to own, so they
        # do not fork; logged below as the fork-tax of every counter in game code).
        living: list[tuple[str, str, str]] = []
        for m in self.company.roster:
            actor = await store.get_actor(fork.branch_id, m.actor_id)
            if actor is not None and actor.status == "alive":
                living.append((m.actor_id, m.name, m.cls))
        living = living[:6]
        self.say(f"  the fork's roster, read back from canon: {', '.join(n for _, n, _ in living)}")
        frictionlog.gap(
            gap="fork the WHOLE game state when fork_branch forks the world",
            happened=(
                "fork_branch forked every Uro projection perfectly — the fork's roster is a "
                "pure canon read (mercs dead only in the finale are alive again here) — but "
                "gold/wins/kill-counters exist only in game code BECAUSE the Reaction Layer "
                "refused to own them, and that shadow state does not fork with the branch"
            ),
            workaround=(
                "roster reconstructed from get_actor(fork).status; the purse/counters are "
                "knowingly wrong on the fork (main-line values leak through)"
            ),
            severity="major",
            needs=(
                "engine-owned numeric state (the WASM/scripting tier) so ALL simulation state "
                "rides the fork — every shadow counter is also a fork-consistency bug"
            ),
            evidence="cli/season.py what_if_fork (roster re-read); game/company.py counters",
        )
        battle = build_battle(contract.scenario, living, enemy_ids, battle_seed)
        report = battle.run()
        names = {u.uid: u.name for u in battle.units}
        self.say(
            f"  [fork battle] {contract.scenario.title} — {report.outcome.upper()} in "
            f"{report.rounds} rounds"
        )
        bundle = chronicle.build_bundle(
            encounter_id=f"e:{contract.contract_id}",
            report=report,
            scenario=contract.scenario,
            names=names,
            site_name=contract.site_name,
            enemy_items={},
        )
        events = await distill_outcome(store, fork.branch_id, bundle)
        commit = await store.append_beat(fork.branch_id, events)
        await self.s.engine.react(fork_campaign, commit.commit_id, events)
        await self.s.engine.agenda_tick(fork.branch_id, contract.travel_days * 2 + 3)

        main = await reads.chronicle_summary(store, self.m.branch_id)
        other = await reads.chronicle_summary(store, fork.branch_id)
        self.say("\n  one chronicle, two histories:")
        self.say(f"    {'':24} {'MAIN (stormed the camp)':34} WHAT-IF (refused)")
        self.say(
            f"    {'war thread':24} {main['threads'].get('t:red-band-war', '?'):34} "
            f"{other['threads'].get('t:red-band-war', '?')}"
        )
        self.say(f"    {'dead in canon':24} {len(main['dead']):<34} {len(other['dead'])}")
        self.say(f"    {'at-war edges':24} {main['wars']!s:34} {other['wars']!s}")
        self.say(f"    {'rumors on record':24} {main['rumor_count']:<34} {other['rumor_count']}")
        only_main = sorted(set(main["dead"]) - set(other["dead"]))
        only_fork = sorted(set(other["dead"]) - set(main["dead"]))
        self.say(f"    dead only on MAIN:    {', '.join(only_main) or '-'}")
        self.say(f"    dead only on WHAT-IF: {', '.join(only_fork) or '-'}")
        self.check(
            "the fork DIVERGED from the same chronicle",
            main["dead"] != other["dead"] or main["threads"] != other["threads"],
        )
        self.record.fork_diff = {
            "main_war": main["threads"].get("t:red-band-war"),
            "fork_war": other["threads"].get("t:red-band-war"),
            "dead_only_main": only_main,
            "dead_only_fork": only_fork,
        }


# -----------------------------------------------------------------------------------------------
# the season, composed
# -----------------------------------------------------------------------------------------------


async def run_season(
    *,
    seed: int,
    posture: str = "embed",
    provider: str = "stub",
    model: str | None = None,
    dsn: str | None = None,
    verbose: bool = False,
) -> SeasonRecord:
    from ironwake.world.uro import DEFAULT_DSN

    session = await UroSession.connect(dsn or DEFAULT_DSN, provider, model)
    server: ServerHandle | None = None
    try:
        marches = await seed_world(session.store, season_seed=seed)
        if posture == "server":
            server = ServerHandle.start(dsn or DEFAULT_DSN)
            log_server_read_gap()
        run = SeasonRun(
            session, marches, seed=seed, posture=posture, server=server, verbose=verbose
        )
        run.say(
            f"IRONWAKE — a season in the Marches (seed {seed}, posture {posture}, "
            f"provider {provider})"
        )
        run.say(f"  world {marches.world.world_id} · campaign {marches.campaign.campaign_id}")
        run.say(
            "  the company: " + ", ".join(f"{m.name} the {m.cls}" for m in marches.company.roster)
        )
        run.counter_wall()

        first_bundle: OutcomeBundle | None = None
        pre_final_commit = ""
        company_alive = True
        for i, contract in enumerate(SEASON):
            is_final = i == len(SEASON) - 1
            if is_final:
                pre_final_commit = (
                    await session.store.get_branch(marches.branch_id)
                ).head_commit or ""
            run.say(
                f"\n=== Contract {i + 1}/{len(SEASON)}: {contract.title} "
                f"({contract.kind} at {contract.site_name}, {contract.pay}g) ==="
            )
            if not company_alive:
                run.say("  The Ironwake Company no longer answers. The contract goes unposted.")
                run.record.outcomes.append(f"{contract.contract_id}:unfought")
                continue
            day = await session.store.current_world_time(marches.branch_id)
            await run.town_phase(f"world day {day}")
            marches.company.contracts_taken += 1

            if contract.kind == "headhunt":
                # taking the bounty is world state, not a game variable: the thread goes active
                await session.store.append_beat(
                    marches.branch_id,
                    [
                        thread_state_changed(
                            thread_id="t:vorlund-bounty", to_state="active", from_state="offered"
                        )
                    ],
                )
            if contract.kind == "stand":
                watch = [
                    (m.actor_id, m.name, m.cls)
                    for m in marches.company.living()[-STAND_WATCH_SIZE:]
                ]
                run.say(
                    "  [march] Skane wants the mill held at any cost. The captain sends "
                    "the newest blades: " + ", ".join(n for _, n, _ in watch)
                )
                mercs = watch
            else:
                mercs = [
                    (m.actor_id, m.name, m.cls)
                    for m in marches.company.deploy(contract.scenario.max_deploy)
                ]
                run.say(
                    f"  [march] {contract.travel_days} days to {contract.site_name}; "
                    f"deployed: {', '.join(n for _, n, _ in mercs)}"
                )
            if contract.kind == "crown" and VORLUND in chronicle.enemy_ids_for(
                contract.contract_id, contract.scenario
            ):
                vorlund = await session.store.get_actor(marches.branch_id, VORLUND)
                if vorlund is not None and vorlund.status == "alive":
                    run.say(
                        "      On the palisade stands Captain Vorlund — the man the ford "
                        "was supposed to have killed. Canon never bought your story."
                    )

            battle_seed = seed * 1000 + i
            report, names, _ = await run.battle_phase(contract, mercs, battle_seed)
            bundle, outcome = await run.report_phase(contract, report, names)
            if first_bundle is None:
                first_bundle = bundle

            if contract.kind == "headhunt":
                await run.dramatize_headhunt(outcome, names)
            if contract.kind == "stand":
                await run.dramatize_silence(contract, report, outcome, names)
            await run.settle_contract(contract, report, outcome, names)
            await run.downtime_phase(contract)

            if i == 0:
                await run.idempotency_probe(bundle)
            enemy_fled = [u for u in report.fled if not u.startswith("a:merc-")]
            if enemy_fled and bundle.feats:
                # a routed enemy is a WITNESS whose knows-edge points at his captain — so if
                # this battle produced feats AND an enemy fled, Vorlund MUST have heard THIS
                # battle's tale (a real assertion on this encounter's deterministic claim ids,
                # not on stale beliefs from an earlier contract)
                prefix = f"c:{bundle.encounter_id}:feat:"
                fresh = [
                    b
                    for b in await session.store.beliefs_of(marches.branch_id, VORLUND)
                    if b.claim_id.startswith(prefix)
                ]
                run.check(
                    "a FLEEING enemy carried THIS battle's legend to his own side", bool(fresh)
                )
                if fresh:
                    claim = await session.store.get_claim(marches.branch_id, fresh[0].claim_id)
                    run.say(
                        f"  [the enemy remembers] a routed survivor reached the Red Band: "
                        f"Vorlund now {reads.certainty_phrase(fresh[0].confidence)} "
                        f'({fresh[0].confidence:.3f}): "{claim.statement if claim else "?"}"'
                    )
            if contract.kind == "headhunt" and VORLUND in outcome.downgraded:
                await run.rumor_ripple("cut down Captain Vorlund")
                mira_lines = await run.narrator_hears(
                    "Mira",
                    "I sit at Mira's bar in Ironwake Hold and ask Mira about the "
                    "Red Captain's fall.",
                )
                corin_lines = await run.narrator_hears(
                    "Corin",
                    "I find Corin the crier at Duns-Ferry and ask Corin about the "
                    "Red Captain's fall.",
                )
                # asserted on the ENGINE's own narrator-prompt rendering (build_narrator_
                # messages), not on this game's mirrored thresholds
                run.check(
                    "the narrator PROMPT hedges by distance: 'Mira believes' the fall",
                    any("Mira believes" in ln for ln in mira_lines),
                )
                run.check(
                    "the narrator PROMPT hedges by distance: 'Corin has heard a rumor' of it",
                    any("Corin has heard a rumor" in ln for ln in corin_lines),
                )
            if i == 4:
                await run.adversarial_probe()

            if not marches.company.living():
                company_alive = False
                run.say("\n  The last of the Ironwake Company is gone. The season ends here.")

        # --- the ending -----------------------------------------------------------------------
        threads = {
            t.thread_id: t.state for t in await session.store.list_threads(marches.branch_id)
        }
        if not company_alive:
            ending = "a grave in the Marches — the company is wiped out"
        elif threads.get("t:red-band-war") == "resolved":
            ending = "crowned — the standard is taken and the Red Band war is RESOLVED in canon"
        elif run.record.outcomes and run.record.outcomes[-1].split(":")[1] == "win":
            ending = "the camp burned, but the war smoulders on"
        else:
            ending = "beaten at the palisade — the war goes on without you"
        run.record.ending = ending
        run.record.gold = marches.company.gold
        run.record.threads = threads

        await run.what_if_fork(pre_final_commit, seed * 1000 + 99)

        # --- the season ledger ------------------------------------------------------------------
        run.say("\n=== SEASON'S END ===")
        run.say(f"  ending: {ending}")
        run.say(
            f"  purse:  {marches.company.gold}g · contracts {marches.company.wins} paid / "
            f"{marches.company.losses} unpaid"
        )
        roster = ", ".join(
            f"{m.name}{'' if m.alive else ' (dead)'}" for m in marches.company.roster
        )
        run.say(f"  the ledger of the company: {roster}")
        summary = await reads.chronicle_summary(session.store, marches.branch_id)
        run.say(
            f"  world day {summary['world_day']} · dead in canon: {len(summary['dead'])} · "
            f"rumors on record: {summary['rumor_count']}"
        )
        run.say(f"  threads: {summary['threads']}")
        run.say("\n  what the taverns will still be telling (a sample of canonized testimony):")
        for statement in summary["rumors"][:6]:
            run.say(f'    - "{statement}"')

        frictionlog.print_refusal_log()
        frictionlog.print_gap_table()

        failed = [label for label, ok in run.record.checks if not ok]
        run.say(
            f"\nSELF-CHECKS: {len(run.record.checks) - len(failed)}/{len(run.record.checks)} passed"
        )
        if failed:
            for label in failed:
                run.say(f"  FAILED: {label}")
            raise SystemExit(1)
        return run.record
    finally:
        if server is not None:
            server.stop()
        await session.close()
