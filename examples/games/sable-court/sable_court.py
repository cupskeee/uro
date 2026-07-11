"""THE SABLE COURT — a court-intrigue realm simulation built ON the Uro engine (Posture A).

You are the Spymaster of Karsis. The King is dying; the Houses scheme. You nudge — a whisper, a
forged letter, a brokered marriage — while the realm turns around you: court beats run through
`engine.run_beat`, downtime turns through `engine.agenda_tick` in lockstep with a numeric shadow
ledger Uro cannot own, and at the end the timeline FORKS: the same event log replayed into a
brokered peace, and the two histories diverge.

Deterministic: a scripted provider serves the prose, one seeded RNG rolls the war dice — no API
key. A real model is opt-in: `--provider openai|anthropic` (needs a key; relaxes the scripted
assertions to observations, since live prose extracts differently).

Run (Postgres on host port 5433; see README.md):

    uv run python examples/games/sable-court/sable_court.py

This game is also a forcing function: every place the engine surprised or refused us is logged
at the call site (frictionlog.py) and printed at the end — the raw material of GAP_REPORT.md.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from typing import Any

import frictionlog
import ledger as ledger_mod
import realm
import script
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle, distill_outcome
from uro_core.domain.events import (
    DomainEvent,
    claim_recorded,
    edge_added,
    edge_removed,
    place_state_changed,
    thread_created,
    thread_state_changed,
)
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.recall import RecallBundle, assemble_recall
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign

DSN = "postgresql://uro:uro@localhost:5433/uro"
SIM_SEED = 5  # the realm's one RNG seed (war dice); frozen so the arc below is the arc

STRICT = True  # scripted mode asserts; live mode (--provider openai|...) only observes


def check(label: str, ok: bool) -> None:
    mark = "✓" if ok else "✗"
    print(f"   {mark} {label}")
    if STRICT and not ok:
        raise AssertionError(f"FAILED: {label}")


def banner(stage: str, title: str) -> None:
    print(f"\n{'═' * 78}\n  STAGE {stage} — {title}\n{'═' * 78}")


HOUSE_TO_FACTION = {
    "crown": "f:crown",
    "vaelric": "f:vaelric",
    "corvane": "f:corvane",
    "dellmoor": "f:dellmoor",
    "argent": "f:argent",
    "ashen": "f:ashen",
}
FACTION_TO_HOUSE = {v: k for k, v in HOUSE_TO_FACTION.items()}

# minor plots the realm spawns during downtime (game-authored; the engine has no plot generator)
THREAD_MILL: dict[int, list[tuple[str, str]]] = {
    1: [
        ("t:widows-of-the-fords", "The war's first widows petition the Throne."),
        ("t:salt-road-tolls", "Toll wars choke the salt road."),
        ("t:levy-desertions", "March levies desert by the dozen."),
    ],
    2: [
        ("t:greyfen-ghosts", "Something in the Greyfen eats the caravans' rearguard."),
        ("t:oldkeep-lien", "The Ledger's lien on Oldkeep scandalizes the old blood."),
        ("t:grain-hoarders", "Grain hoarders bleed the capital's markets."),
    ],
    3: [
        ("t:march-refugees", "Refugees from the march camp outside the walls."),
        ("t:crown-taster", "The King's new taster reports to somebody."),
        ("t:harbor-fires", "Fires on the Saltport harbor front, twice in a week."),
    ],
}


class SableCourt:
    """The game: owns the store, the engines, the shadow ledger, and the whole scripted arc."""

    def __init__(self, store: PostgresEventStore, router: ProviderRouter | None = None) -> None:
        self.store = store
        self.router_override = router  # live mode (--provider openai|…); None = scripted
        self.ledger = ledger_mod.ShadowLedger(SIM_SEED)
        self.battle_no = 0
        self.thread_counts: list[int] = []
        self.world_id = ""
        self.branch = ""
        self.campaign: Campaign
        self.engine: Engine
        self.fork_commit = ""  # head after tick 1 — the fork point
        self.ledger_at_fork: ledger_mod.ShadowLedger | None = None

    # --- plumbing -------------------------------------------------------------------------

    async def commit_authored(
        self, campaign: Campaign, events: list[DomainEvent], *, react_with: Engine
    ) -> str:
        """Append game-authored events AND run the Reaction Layer over them.

        `append_beat` alone does NOT run reactions — only `Engine._finish` (player beats) and
        the server's outcome endpoint call `react`. An embedding consumer reflecting external
        facts must remember to call `engine.react` itself or its reaction rules are silently
        dead for those events. Logged once below (a real footgun: we lost R1 to it until this
        helper existed).
        """
        commit = await self.store.append_beat(campaign.branch_id, events)
        await react_with.react(campaign, commit.commit_id, events)
        return commit.commit_id

    async def module_rumors(self, branch: str) -> list[str]:
        # deduped: each agenda cadence re-mints the SAME rumor text as a fresh claim (ids key on
        # the trigger commit), so a long war accumulates identical rumors — see RL "rumor decay"
        return sorted(
            {
                c.statement
                for c in await self.store.list_claims(branch)
                if c.truth == "unknown" and c.origin == "module"
            }
        )

    async def thread_state(self, branch: str, thread_id: str) -> str:
        for t in await self.store.list_threads(branch):
            if t.thread_id == thread_id:
                return t.state
        return "(missing)"

    async def war_edges(self, branch: str) -> list[tuple[str, str]]:
        return sorted((e.src, e.dst) for e in await self.store.list_edges(branch, "at_war_with"))

    async def sync_wars_from_uro(self, branch: str, ledger: ledger_mod.ShadowLedger) -> None:
        """Reverse-reflection: wars the REACTION LAYER declares (r5a on the fork) exist only as
        Uro edges — the shadow ledger can't see them. Two sources of truth ⇒ the game must sync
        both ways. (Half of gap G-shadow-state; the forward half is reflect_tick below.)"""
        for src, dst in await self.war_edges(branch):
            a, b = FACTION_TO_HOUSE.get(src), FACTION_TO_HOUSE.get(dst)
            if a and b:
                ledger.at_war.add((a, b) if a < b else (b, a))

    # --- the court phase ------------------------------------------------------------------

    async def play_beat(self, engine: Engine, campaign: Campaign, intent: str) -> Any:
        result = await engine.run_beat(campaign, "player-1", intent)
        print(f'\n » "{intent}"')
        print(f"   {result.narration}")
        print(f"   [committed: extracted {result.extracted} state event(s)]")
        return result

    # --- the downtime phase ---------------------------------------------------------------

    async def run_tick(
        self,
        days: int,
        engine: Engine,
        campaign: Campaign,
        ledger: ledger_mod.ShadowLedger,
        *,
        mill: list[tuple[str, str]] | None = None,
        battles_plan: dict[int, str] | None = None,
    ) -> ledger_mod.TickReport:
        branch = campaign.branch_id
        print(f"\n ── downtime: {days} days pass ──")
        # 1. Uro's own downtime: advance world time + fire the pack's agenda rules (no LLM).
        await engine.agenda_tick(branch, days)
        # 2. Reverse-sync wars the agendas may have declared (r5a) into the shadow ledger.
        await self.sync_wars_from_uro(branch, ledger)
        # 3. The numeric realm turns — arithmetic Uro cannot own (see ledger.py's refusal log).
        rep = ledger.tick(days)
        day = await self.store.current_world_time(branch)
        print(f"   world day {day} · ledger day {rep.day}")
        for line in rep.income_lines:
            print(f"   income   · {line}")
        for line in rep.ambition_lines:
            print(f"   ambition · {line}")
        # 4. Reflect the qualitative outcomes back into Uro canon.
        await self.reflect_tick(rep, engine, campaign, battles_plan or {})
        # 5. The realm breeds plots faster than it retires them (thread-lifecycle stress).
        for tid, stakes in mill or []:
            await self.store.append_beat(
                branch, [thread_created(thread_id=tid, stakes=stakes, state="active")]
            )
            print(f"   new plot · {stakes}")
        if branch == self.branch:  # the monotonic-growth audit tracks the main line only
            self.thread_counts.append(len(await self.store.list_threads(branch)))
        return rep

    async def reflect_tick(
        self,
        rep: ledger_mod.TickReport,
        engine: Engine,
        campaign: Campaign,
        battles_plan: dict[int, str],
    ) -> None:
        branch = campaign.branch_id
        for a, b in rep.wars_declared:
            fa, fb = HOUSE_TO_FACTION[a], HOUSE_TO_FACTION[b]
            print(f"   WAR      · {a} ↔ {b} (tension boiled over)")
            await self.commit_authored(
                campaign,
                [
                    edge_added(src=fa, rel_type="at_war_with", dst=fb),
                    edge_added(src=fb, rel_type="at_war_with", dst=fa),
                    thread_created(
                        thread_id=f"t:war-{a}-{b}",
                        stakes=f"Open war between House {a.title()} and House {b.title()}.",
                        state="active",
                    ),
                    claim_recorded(
                        claim_id=f"c:war-{a}-{b}",
                        statement=f"House {a.title()} and House {b.title()} are at war.",
                        subject_refs=[fa, fb],
                        truth="true",
                        origin="game-sim",
                    ),
                ],
                react_with=engine,
            )
        for battle in rep.battles:
            await self.fight_battle(battle, engine, campaign, battles_plan)
        for house, place, buyer in rep.distress_sales:
            fh, fb = HOUSE_TO_FACTION[house], HOUSE_TO_FACTION[buyer]
            print(f"   SALE     · {house} pawns {place} to {buyer}")
            await self.transfer_holding(
                campaign,
                engine,
                place,
                fh,
                fb,
                f"House {house.title()} pawned {self.place_name(place)} to the Argent Ledger.",
            )
        for house in rep.landless:
            print(f"   LANDLESS · House {house} holds nothing now")
            await self.store.append_beat(
                branch,
                [
                    claim_recorded(
                        claim_id=f"c:landless-{house}",
                        statement=f"House {house.title()} is landless; its banners fly over "
                        "nothing.",
                        subject_refs=[HOUSE_TO_FACTION[house]],
                        truth="true",
                        origin="game-sim",
                    )
                ],
            )

    def place_name(self, place_id: str) -> str:
        for pid, name, _, _ in realm.PLACES:
            if pid == place_id:
                return name
        return place_id

    async def transfer_holding(
        self,
        campaign: Campaign,
        engine: Engine,
        place: str,
        from_faction: str,
        to_faction: str,
        why: str,
    ) -> None:
        """A holding changes hands: flip the `owns` edge, update the place description, and
        record the fact TWICE — once keyed to the place id (which recall can never surface)
        and once keyed to a name-token (which it can). The pair is the receipt for the
        place-state recall gap (§3 target 4)."""
        new_holder = self.place_name_of_faction(to_faction)
        events = [
            edge_removed(src=from_faction, rel_type="owns", dst=place),
            edge_added(src=to_faction, rel_type="owns", dst=place),
            place_state_changed(
                place_id=place,
                changes={"description": f"{why.rstrip('.')}. Now held by {new_holder}."},
            ),
            claim_recorded(
                claim_id=f"c:transfer-{place[2:]}-{to_faction[2:]}",
                statement=f"{self.place_name(place)} changed hands: {why}",
                subject_refs=[place],  # ← keyed to the place id: recall is blind to this
                truth="true",
                origin="game-sim",
            ),
            claim_recorded(
                claim_id=f"c:transfer-{place[2:]}-{to_faction[2:]}-token",
                statement=f"{self.place_name(place)} is now held by {new_holder}.",
                # keyed to a name-token, the extractor's convention — THIS one recall can find
                subject_refs=[f"name:{self.place_name(place).lower()}"],
                truth="true",
                origin="game-sim",
            ),
        ]
        await self.commit_authored(campaign, events, react_with=engine)

    def place_name_of_faction(self, faction: str) -> str:
        for fid, name, _ in realm.FACTIONS:
            if fid == faction:
                return name
        return faction

    async def fight_battle(
        self,
        battle: ledger_mod.Battle,
        engine: Engine,
        campaign: Campaign,
        battles_plan: dict[int, str],
    ) -> None:
        """A war-tick battle, resolved numerically by the ledger and reported to Uro through
        the Chronicler contract — the external-game posture, exercised head-on."""
        branch = campaign.branch_id
        self.battle_no += 1
        kind = battles_plan.get(self.battle_no, "skirmish")
        print(
            f"   BATTLE   · {battle.attacker} {battle.rolls[0]} vs {battle.defender} "
            f"{battle.rolls[1]} → {battle.winner} prevails"
            + (f"; {battle.loser} cedes {battle.ceded_holding}" if battle.ceded_holding else "")
        )
        if kind == "salt-road":
            bundle = OutcomeBundle(
                encounter_id="e:salt-road",
                participants=[
                    "a:tobbin",
                    "a:willem",
                    "a:ser-garret",
                    "a:ser-garrick",
                    "a:aldric-vaelric",
                ],
                witnesses=["a:willem", "a:ser-garrick"],
                # Tobbin (T0 levy) dies for real; the Marshal (T3) is our deliberate collision
                # with the protection ceiling — Uro must downgrade him to a rumor.
                casualties=["a:tobbin", "a:aldric-vaelric"],
                feats=[
                    Feat(
                        actor="a:ser-garrick",
                        description="Ser Garrick held the salt causeway alone against the "
                        "Vaelric van.",
                    )
                ],
                loot=[
                    LootTransfer(
                        item_id="i:vaelric-warbanner", from_ref="a:tobbin", to_ref="a:ser-garrick"
                    ),
                    # the Marshal's letters: a protected lord's gear — Uro must refuse this
                    LootTransfer(
                        item_id="i:marshals-letters",
                        from_ref="a:aldric-vaelric",
                        to_ref="a:ser-garrick",
                    ),
                ],
                duration_rounds=6,
            )
        elif kind == "night-ambush":
            bundle = OutcomeBundle(
                encounter_id="e:night-ambush",
                participants=["a:willem", "a:ser-garret"],
                witnesses=[],  # nobody lived to tell — the world must stay silent
                casualties=["a:willem"],
                feats=[
                    Feat(
                        actor="a:ser-garret",
                        description="Ser Garret cut down the Greyfen rearguard in the dark.",
                    )
                ],
                loot=[],
                duration_rounds=2,
            )
        elif kind == "march-storm":
            bundle = OutcomeBundle(
                encounter_id="e:march-storm",
                participants=["a:ser-gareth", "a:ser-garret", "a:captain-hurn"],
                witnesses=["a:ser-garret", "a:captain-hurn"],
                casualties=["a:ser-gareth"],  # a T1 sworn knight CAN die — below the ceiling
                feats=[
                    Feat(
                        actor="a:ser-garret",
                        description="Ser Garret unhorsed Ser Gareth at the storm-gate of Oldkeep.",
                    )
                ],
                loot=[],
                duration_rounds=4,
            )
        else:
            bundle = OutcomeBundle(
                encounter_id=f"e:skirmish-{self.battle_no}",
                participants=["a:ser-garret", "a:ser-garrick", "a:captain-hurn"],
                witnesses=["a:captain-hurn"],
                casualties=[],
                feats=[
                    Feat(
                        actor="a:ser-garret",
                        description="Ser Garret held the Weeping Bridge until the horns "
                        "called him back.",
                    )
                ],
                loot=[],
                duration_rounds=3,
            )
        events = await distill_outcome(self.store, branch, bundle)
        commit = await self.store.append_beat(branch, events)
        await engine.react(campaign, commit.commit_id, events)  # deaths must reach the rules
        kinds = sorted(e.event_type for e in events)
        print(f"     chronicled as: {', '.join(kinds) if kinds else '(nothing survived scope)'}")
        if self.battle_no == 1:  # logged once, at the first bundle
            frictionlog.gap(
                gap="Tie a battle's outcome to WHEN it happened in world time",
                happened="an OutcomeBundle carries no time; its events commit at whatever "
                "world_time the branch happens to hold (here: the day agenda_tick already "
                "advanced to). duration_rounds is decorative — there is no game↔world time "
                "mapping (known limit #2, confirmed hit)",
                workaround="the game orders its calls carefully (tick first, battles after) so "
                "the day is right by construction",
                severity="annoyance",
                needs="an optional world_time (or day offset) on the bundle / distill_outcome",
                evidence="uro_core/chronicler.py OutcomeBundle (no time field); "
                "sable_court.py run_tick call order",
            )
        if battle.ceded_holding:
            winner_f = HOUSE_TO_FACTION[battle.winner]
            loser_f = HOUSE_TO_FACTION[battle.loser]
            await self.transfer_holding(
                campaign,
                engine,
                battle.ceded_holding,
                loser_f,
                winner_f,
                f"ceded by House {battle.loser.title()} after the battle",
            )

    # ======================================================================================
    # The run — stages 0..7, each printing its verifications.
    # ======================================================================================

    async def run(self) -> None:
        await self.stage0_skeleton()
        await self.stage1_realm()
        await self.stage2_court()
        await self.stage3_downtime()
        await self.stage4_scale()
        await self.stage5_fork()
        self.stage6_refusals()
        self.stage7_gaps()

    async def stage0_skeleton(self) -> None:
        banner("0", "SKELETON & DETERMINISM — the engine comes up, one beat commits")
        pairs = [(n, x) for _, n, x in script.MAIN_BEATS]
        # the assassination is previewed (dry-run) then committed: two provider servings
        pairs.append((script.ASSASSINATION_BEAT[1], script.ASSASSINATION_BEAT[2]))
        pairs.append((script.ASSASSINATION_BEAT[1], script.ASSASSINATION_BEAT[2]))
        router = self.router_override or ProviderRouter(
            bindings={}, default=script.ScriptedProvider(pairs)
        )
        self.engine = Engine(self.store, router)
        world = await self.store.create_world(
            "Karsis",
            tone=["baroque", "conspiratorial", "cold"],
            rule_pack=realm.RULE_PACK,
            extra_events=realm.seed_events(),
        )
        self.world_id = world.world_id
        self.branch = world.main_branch_id
        self.campaign = await self.store.start_campaign(
            self.world_id,
            self.branch,
            participant_id="player-1",
            new_pc_name="the Spymaster",
            new_pc_id=realm.SPYMASTER,
        )
        result = await self.play_beat(self.engine, self.campaign, script.MAIN_BEATS[0][0])
        self.ledger.add_tension("vaelric", "corvane", 1, "the whisper")
        check("runs with no API key (scripted provider)", True)
        check("BeatResult.commit_id is non-empty", bool(result.commit_id))
        check("beat extracted durable state (1 claim + 1 belief)", result.extracted >= 2)
        print("   (ids are never printed: a clean-DB re-run is byte-identical)")

    async def stage1_realm(self) -> None:
        banner("1", "THE REALM AS SEEDED CANON — cast, geography, plots, all in Uro")
        actors = await self.store.list_actors(self.branch)
        factions = await self.store.list_factions(self.branch)
        places = await self.store.list_places(self.branch)
        threads = await self.store.list_threads(self.branch)
        owns = await self.store.list_edges(self.branch, "owns")
        knows = await self.store.list_edges(self.branch, "knows")
        self.thread_counts.append(len(threads))
        print(
            f"   {len(actors)} actors · {len(factions)} factions · {len(places)} places · "
            f"{len(threads)} threads · {len(owns)} holdings · {len(knows)} gossip edges"
        )
        check("≥ 14 actors seeded (incl. the confusable-name set)", len(actors) >= 15)
        check("≥ 6 factions (+ the umbrella court)", len(factions) >= 7)
        check("≥ 6 places, each owned by a House", len(places) >= 7 and len(owns) >= 7)
        check("≥ 12 threads in mixed states", len(threads) >= 12)
        marshal = await self.store.find_actor_by_name(self.branch, "the Marshal")
        aldric = await self.store.find_actor_by_name(self.branch, "Aldric")
        younger = await self.store.find_actor_by_name(self.branch, "the Younger")
        lady = await self.store.find_actor_by_name(self.branch, "Lady Corvane")
        check(
            "alias resolution: 'the Marshal' → Aldric Vaelric",
            marshal is not None and marshal.actor_id == "a:aldric-vaelric",
        )
        check(
            "alias tiebreak: bare 'Aldric' → the Marshal (not the Younger)",
            aldric is not None and aldric.actor_id == "a:aldric-vaelric",
        )
        check(
            "alias resolution: 'the Younger' → Aldric the Younger",
            younger is not None and younger.actor_id == "a:aldric-younger",
        )
        check(
            "alias resolution: 'Lady Corvane' → Aldrice Corvane",
            lady is not None and lady.actor_id == "a:aldrice-corvane",
        )

    async def stage2_court(self) -> None:
        banner("2", "COURT BEATS — intrigue verbs through run_beat; reaction rules fire")
        for i, (intent, _, _) in enumerate(script.MAIN_BEATS[1:], start=2):
            await self.play_beat(self.engine, self.campaign, intent)
            if i == 5:
                t = self.ledger.add_tension("vaelric", "corvane", 2, "the sold letters")
                print(f"   [shadow ledger: vaelric↔corvane tension → {t}]")
            if i == 6:
                t = self.ledger.add_tension("vaelric", "corvane", 2, "the forged raid order")
                self.ledger.mark_unready("vaelric", 4)
                print(f"   [shadow ledger: vaelric↔corvane tension → {t}; vaelric unready]")
            if i == 7:
                # the betrothal: an alliance the world should own → authored edges + reactions
                print("   [reflecting the betrothal into canon: allied_with edges]")
                await self.commit_authored(
                    self.campaign,
                    [
                        edge_added(src="f:vaelric", rel_type="allied_with", dst="f:dellmoor"),
                        edge_added(src="f:dellmoor", rel_type="allied_with", dst="f:vaelric"),
                    ],
                    react_with=self.engine,
                )
                self.ledger.reset_tension("vaelric", "dellmoor")
        frictionlog.gap(
            gap="Reflecting sim results via append_beat should trigger the Reaction Layer",
            happened="append_beat commits and projects but never runs rules; only Engine._finish "
            "(player beats) and the server outcome route call react() — our R1/R4 rules were "
            "silently dead for authored/Chronicler events until the game called engine.react "
            "itself",
            workaround="every game append goes through commit_authored(), which calls "
            "engine.react(campaign, commit_id, events) manually (mirrors uro-server)",
            severity="major",
            needs="a store- or engine-level 'append with reactions' entry point (or react() "
            "folded into append_beat behind a flag) so embedders can't forget the second call",
            evidence="sable_court.py commit_authored; uro_core/pipeline/engine.py:315 _finish "
            "vs adapters/postgres/store.py:769 append_beat",
        )
        counter = await self.thread_state(self.branch, "t:counter-pact")
        rumors = await self.module_rumors(self.branch)
        guild_rumor = [r for r in rumors if "Ledger reprices" in r]
        check(
            "REACTION (post-beat): allied_with edge → r4a created t:counter-pact",
            counter == "dormant",
        )
        check(
            "REACTION (post-beat): r4b's rumor landed as a truth=unknown module claim",
            len(guild_rumor) == 1,
        )
        if guild_rumor:
            print(f'     the rumor: "{guild_rumor[0]}"')
        frictionlog.refusal(
            name="One rule touching a thread AND a faction (the scope split)",
            wished_rule="""{ "id": "alliance-echoes",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "allied_with"}},
  "then": [{"do": "create_thread", "thread": "t:counter-pact",
            "stakes": "A counter-pact forms against the new alliance."},
           {"do": "record_rumor", "text": "The Ledger reprices every debt by dawn.",
            "subjects": ["a:maren-argent"]}],
  "scope": {"thread": "t:counter-pact", "faction": "f:argent"} }  // ← TWO scopes: forbidden""",
            missing="multi-dimension scope — Scope allows exactly one of thread|faction|place, so "
            "this one reaction ships as two rules (r4a thread-scoped + r4b faction-scoped) that "
            "can drift apart",
            where="realm.py RULE_PACK r4a/r4b (the split we actually shipped)",
        )
        # entity-resolution audit of the beats just played
        marshal_claims = await self.store.claims_about(self.branch, "a:aldric-vaelric")
        check(
            "extractor resolved 'the Marshal'/'Aldric' claims onto Aldric Vaelric via aliases",
            len(marshal_claims) >= 2,
        )
        salt_knight = await self.store.find_actor_by_name(self.branch, "the Salt Knight")
        check(
            "FRAGMENTATION (expected): unaliased 'the Salt Knight' minted a NEW actor",
            salt_knight is not None
            and salt_knight.actor_id.startswith("a:")
            and salt_knight.tier == 1,
        )
        frictionlog.gap(
            gap="An unaliased colloquial handle ('the Salt Knight' — actually Ser Garrick) "
            "should resolve, or at least be mergeable once discovered",
            happened="canonical-name + alias matching found no match, so the gauntlet minted a "
            "brand-new T1 actor; there is no merge primitive and no alias-add event to repair "
            "it after the fact",
            workaround="author aliases up front for every colloquial handle (done for 15 nobles);"
            " the Salt Knight stays fragmented as the exhibit",
            severity="annoyance",
            needs="an AliasAdded event (cheap) and/or the deferred embedding entity_index "
            "(OQ-3) plus an actor-merge event for post-hoc repair",
            evidence="script.py beat 8 extraction; uro_core/pipeline/extraction.py:142 resolve()",
        )

    async def stage3_downtime(self) -> None:
        banner("3", "DOWNTIME — agenda_tick + the shadow ledger; the realm turns by itself")
        # ── tick 1: tension is already at the threshold; war ignites, first blood spills ──
        await self.run_tick(
            20,
            self.engine,
            self.campaign,
            self.ledger,
            mill=THREAD_MILL[1],
            battles_plan={1: "salt-road"},
        )
        feud = await self.thread_state(self.branch, "t:vaelric-corvane-feud")
        tobbin = await self.store.get_actor(self.branch, "a:tobbin")
        marshal = await self.store.get_actor(self.branch, "a:aldric-vaelric")
        marshal_rumor = [
            c.statement
            for c in await self.store.claims_about(self.branch, "a:aldric-vaelric")
            if c.truth == "unknown" and "fallen" in c.statement
        ]
        banner_item = await self.store.get_item(self.branch, "i:vaelric-warbanner")
        letters = await self.store.get_item(self.branch, "i:marshals-letters")
        check(
            "war reflected: at_war_with(f:vaelric, f:corvane) edge exists",
            (("f:corvane", "f:vaelric") in await self.war_edges(self.branch)),
        )
        check(
            "REACTION (thread flip): ActorDied woke the feud (dormant → active) via r1",
            feud == "active",
        )
        check(
            "Chronicler: Tobbin (T0 declared combatant) truly died",
            tobbin is not None and tobbin.status == "dead",
        )
        check(
            "TRUST CEILING: the Marshal (T3) was NOT killed — no committed death",
            marshal is not None and marshal.status == "alive",
        )
        check(
            "TRUST CEILING: his 'death' downgraded to a truth=unknown rumor",
            len(marshal_rumor) == 1,
        )
        if marshal_rumor:
            print(f'     the rumor: "{marshal_rumor[0]}"')
        check(
            "loot: the war-banner really moved (T0 loser owned it)",
            banner_item is not None and banner_item.get("owner_ref") == "a:ser-garrick",
        )
        check(
            "TRUST CEILING: the protected Marshal's letters did NOT move",
            letters is not None and letters.get("owner_ref") == "a:aldric-vaelric",
        )
        frictionlog.gap(
            gap="A great lord (tier ≥ 2) should be able to die in a battle the realm sim "
            "resolved — this game is ABOUT killing great lords",
            happened="distill_outcome downgraded the Marshal's casualty to a truth=unknown "
            "'is said to have fallen' claim and dropped the loot of his letters; only the T0 "
            "levy died. By design (D-32) — but it walls off the game's central fantasy: no "
            "external/sim path can ever kill named canon",
            workaround="the game treats great-lord deaths as unconfirmable rumors "
            "(surprisingly good court-intrigue flavor) — but TRUE succession-by-assassination "
            "is unreachable: BLOCKED",
            severity="major",
            needs="a trusted-consumer tier for embedders (Posture A holds root anyway via "
            "append_beat!) — e.g. distill_outcome(trust='embedder') that may kill T2+; or a "
            "sanctioned lethal authored-event path with its own gauntlet",
            evidence="uro_core/chronicler.py:148 _is_protected downgrade; sable_court.py "
            "fight_battle 'salt-road' bundle",
        )
        # the feat's rumor cascade: who has heard of Garrick's stand, at what confidence?
        feat_claims = [
            c
            for c in await self.store.claims_about(self.branch, "a:ser-garrick")
            if "causeway" in c.statement
        ]
        if feat_claims:
            cid = feat_claims[0].claim_id
            heard: list[str] = []
            for actor in await self.store.list_actors(self.branch):
                for b in await self.store.beliefs_of(self.branch, actor.actor_id):
                    if b.claim_id == cid:
                        heard.append(f"{actor.name} ({b.confidence:.2f})")
            heard.sort()
            print(f"   the feat travels the gossip web: {'; '.join(heard)}")
            check("belief propagation: the tale decayed hop by hop to the court", len(heard) >= 3)

        # THE FORK POINT: war just declared, first blood spilled, no downtime rumors yet.
        # Uro will fork its whole state from this commit in stage 5 — but the shadow ledger
        # will NOT (it is game state), so we must snapshot it by hand at the same instant.
        branch_info = await self.store.get_branch(self.branch)
        assert branch_info is not None and branch_info.head_commit is not None
        self.fork_commit = branch_info.head_commit
        self.ledger_at_fork = copy.deepcopy(self.ledger)

        # ── tick 2: the war grinds on; the granary rumor agenda fires; Dellmoor pawns ──
        await self.run_tick(
            20,
            self.engine,
            self.campaign,
            self.ledger,
            mill=THREAD_MILL[2],
            battles_plan={2: "night-ambush"},
        )
        rumors = await self.module_rumors(self.branch)
        granary = [r for r in rumors if "granaries" in r]
        check(
            "AGENDA (r2): the war-breeds-rumor cadence fired — a module rumor about Lady Corvane",
            len(granary) >= 1,
        )
        vey_beliefs = await self.store.beliefs_of(self.branch, "a:mother-vey")
        heresy = [b for b in vey_beliefs if b.claim_id == realm.HERESY_CLAIM_ID]
        issolde = [
            b
            for b in await self.store.beliefs_of(self.branch, "a:queen-issolde")
            if b.claim_id == realm.HERESY_CLAIM_ID
        ]
        check("AGENDA (r3): the heresy spread to the Veil's own", len(heresy) == 1)
        check(
            "…and leaked one hop beyond the cult, into the palace (the Queen has heard)",
            len(issolde) == 1 and issolde[0].confidence < 0.6,
        )
        # the night ambush: zero witnesses ⇒ the world stays silent about the feat
        ambush_feat = [
            c
            for c in await self.store.list_claims(self.branch)
            if "rearguard in the dark" in c.statement
        ]
        silent = True
        if ambush_feat:
            for actor in await self.store.list_actors(self.branch):
                for b in await self.store.beliefs_of(self.branch, actor.actor_id):
                    if b.claim_id == ambush_feat[0].claim_id:
                        silent = False
        willem = await self.store.get_actor(self.branch, "a:willem")
        check(
            "zero witnesses: Willem died and NOBODY believes the ambush tale (no propagation)",
            silent and willem is not None and willem.status == "dead",
        )

        # ── tick 3: attrition ──
        await self.run_tick(
            30,
            self.engine,
            self.campaign,
            self.ledger,
            mill=THREAD_MILL[3],
            battles_plan={},
        )

        # ── the knife in the dark: the trust model hit head-on, on purpose ──
        print("\n ── the knife in the dark ──")
        preview = await self.engine.preview_beat(
            self.campaign, "player-1", script.ASSASSINATION_BEAT[0]
        )
        print(f"   [preview_beat (dry-run): {len(preview)} would-be event(s), nothing committed]")
        await self.play_beat(self.engine, self.campaign, script.ASSASSINATION_BEAT[0])
        bundle = OutcomeBundle(
            encounter_id="e:knife-in-the-dark",
            participants=["a:halric", "a:lys"],
            witnesses=["a:lys"],
            casualties=["a:halric"],
            feats=[
                Feat(
                    actor="a:lys",
                    description="Lys slipped past the Kingsguard into the royal bedchamber unseen.",
                )
            ],
        )
        events = await distill_outcome(self.store, self.branch, bundle)
        commit = await self.store.append_beat(self.branch, events)
        await self.engine.react(self.campaign, commit.commit_id, events)
        await self.store.append_beat(
            self.branch,
            [
                thread_created(
                    thread_id="t:knife-in-the-dark",
                    stakes="Who sent the knife that missed the King?",
                    state="active",
                )
            ],
        )
        king = await self.store.get_actor(self.branch, "a:halric")
        king_rumor = [
            c.statement
            for c in await self.store.claims_about(self.branch, "a:halric")
            if c.truth == "unknown" and "fallen" in c.statement
        ]
        succession = await self.thread_state(self.branch, "t:succession")
        check(
            "the King (T3) survives the bundle — no ActorDied was committed",
            king is not None and king.status == "alive",
        )
        check("the assassination became a truth=unknown rumor instead", len(king_rumor) == 1)
        if king_rumor:
            print(f'     the rumor: "{king_rumor[0]}"')
        check(
            "so r6 (succession-opens-on-kings-death) is authored but UNFIREABLE: still dormant",
            succession == "dormant",
        )
        frictionlog.refusal(
            name="Succession that can actually open (a death the trust model permits)",
            wished_rule="""{ "id": "r6-succession-opens-on-kings-death",  // SHIPPED — dead code:
  "trigger": {"event": "ActorDied", "where": {"actor_id": "a:halric"}},
  "when": {"kind": "thread_state", "thread": "t:succession", "state": "dormant"},
  "then": [{"do": "set_thread_state", "thread": "t:succession", "to": "active"}],
  "scope": {"thread": "t:succession"} }""",
            missing="any runtime path that can emit ActorDied for a T2+ actor: combat is "
            "non-lethal, the Chronicler ceiling downgrades, rules can't kill — so a rule "
            "triggered on a protected actor's death can never fire (the grammar's trigger "
            "vocabulary outruns what the trust model lets happen)",
            where="realm.py RULE_PACK r6 + sable_court.py stage3 assassination asserts",
        )

    async def stage4_scale(self) -> None:
        banner("4", "SCALE STRESS — dozens of plots, a blind narrator, a crowded court")
        threads = await self.store.list_threads(self.branch)
        print(f"   thread counts after each phase: {self.thread_counts} → now {len(threads)}")
        check("threads grew to dozens (≥ 24)", len(threads) >= 24)
        check(
            "growth is monotonic — nothing ever retires a plot",
            all(a <= b for a, b in zip(self.thread_counts, self.thread_counts[1:], strict=False)),
        )
        # the ONLY close path is the consumer writing the lifecycle event itself:
        await self.store.append_beat(
            self.branch,
            [thread_state_changed(thread_id="t:tax-revolt", to_state="resolved")],
        )
        check(
            "close path exists but is consumer-driven: we resolved t:tax-revolt by hand",
            await self.thread_state(self.branch, "t:tax-revolt") == "resolved",
        )
        recall = await assemble_recall(
            self.store, self.branch, "What is stirring at court tonight?", 8
        )
        print(
            f"   assemble_recall: {len(recall.active_threads)} live plots go into EVERY "
            f"narrator prompt"
        )
        check("recall still returns at this scale", len(recall.active_threads) >= 15)
        frictionlog.gap(
            gap="Thread lifecycle management at dozens of live plots",
            happened="every active/offered thread is injected into every narrator prompt "
            "(recall.active_threads is campaign-wide, unscoped); nothing in the engine ever "
            "retires, expires, or ranks a plot — the prompt section only ever grows",
            workaround="the game resolves threads by hand-appending ThreadStateChanged; no "
            "relevance ranking is possible from outside",
            severity="major",
            needs="thread relevance scoping in recall (entity-linked or embedding-ranked) and "
            "a lifecycle policy (age-out, cap, or rule-drivable resolve)",
            evidence="uro_core/pipeline/recall.py:96 (active_threads unscoped); "
            "sable_court.py stage4 recall probe",
        )

        # ── the place-state recall probe (§3 target 4) ──
        print("\n ── the border-march probe: does the narrator know it changed hands? ──")
        place = await self.store.get_place(self.branch, "p:border-march")
        owner = [
            e.src
            for e in await self.store.list_edges(self.branch, "owns")
            if e.dst == "p:border-march"
        ]
        assert place is not None
        print(f"   Uro state: owner={owner[0] if owner else '?'} · “{place.description}”")
        intent = "I ride to the Border March — whose banners fly there now?"
        recall = await assemble_recall(self.store, self.branch, intent, 8)
        statements = [c.statement for c in recall.claims]
        id_keyed_visible = any("changed hands" in s for s in statements)
        token_keyed_visible = any("is now held by" in s for s in statements)
        fields = sorted(RecallBundle.model_fields)
        print(f"   RecallBundle fields: {fields} — no place channel exists")
        check(
            "the place-id-keyed claim about the transfer is INVISIBLE to recall",
            not id_keyed_visible,
        )
        check(
            "the same fact keyed to a name-token IS recalled (the only workaround)",
            token_keyed_visible,
        )
        frictionlog.gap(
            gap="The narrator should know p:border-march changed hands (it's in proj_places "
            "and the owns graph)",
            happened="assemble_recall has NO place channel (RecallBundle: beats/actors/claims/"
            "beliefs/memories/threads), and claim relevance only matches actor ids and "
            "name-tokens — a claim whose subject_refs=['p:border-march'] is unreachable even "
            "when the intent names the place",
            workaround="record every place fact TWICE: once keyed to the place id (for state) "
            "and once to a name-token (for recall) — a duplication smell",
            severity="major",
            needs="a place-state recall channel (docs already list this as known-limit #8) and "
            "entity-ref matching for p:/f: refs in claim relevance",
            evidence="uro_core/pipeline/recall.py:73 relevant() / RecallBundle; "
            "sable_court.py transfer_holding (the duplicated claims)",
        )
        # a crowded court: how many actors is entity resolution now juggling?
        actors = await self.store.list_actors(self.branch)
        seeded = len(realm.ACTORS) + 1  # + the PC
        print(f"   actors now: {len(actors)} (seeded {seeded}; the extractor minted the rest)")
        check(
            "exactly one fragment (the Salt Knight) — aliases held for everyone authored",
            len(actors) == seeded + 1,
        )

    async def stage5_fork(self) -> None:
        banner("5", "THE FORK — one event log, two histories: war vs the brokered peace")
        frictionlog.gap(
            gap="fork_branch should fork the WHOLE game state",
            happened="the fork copies every Uro projection at the commit — but the shadow "
            "ledger (strength/gold/tension/at_war) lives in game code, so the game must "
            "snapshot and restore it manually, aligned to the exact fork commit; miss the "
            "snapshot and the two lines silently share numeric fate",
            workaround="copy.deepcopy(self.ledger) taken at the same moment fork_commit is "
            "captured (stage 3); fragile — every potential fork point needs one",
            severity="major",
            needs="the strongest argument FOR engine-owned computation (D-33 Stage B): state "
            "the engine owns forks for free; every number forced into game code breaks the "
            "signature feature",
            evidence="sable_court.py stage3 (ledger_at_fork deepcopy) + stage5 restore",
        )
        fork = await self.store.fork_branch(self.world_id, self.fork_commit, "the-brokered-peace")
        fork_ledger = self.ledger_at_fork
        assert fork_ledger is not None
        fork_provider = script.ScriptedProvider([(n, x) for _, n, x in script.FORK_BEATS])
        fork_router = self.router_override or ProviderRouter(bindings={}, default=fork_provider)
        fork_engine = Engine(self.store, fork_router)
        fork_campaign = await self.store.start_campaign(
            self.world_id,
            fork.branch_id,
            participant_id="player-1",
            adopt_actor_id=realm.SPYMASTER,
        )
        frictionlog.gap(
            gap="Keep playing the same campaign on a fork",
            happened="a Campaign is pinned to its branch_id; the forked branch carries the PC "
            "binding but run_beat on the old campaign would commit to the OLD branch — the "
            "game must start a second campaign on the fork and re-adopt the PC",
            workaround="start_campaign(fork_branch, adopt_actor_id='a:spymaster') — works, "
            "but is undocumented as THE fork-play pattern and emits a second CampaignStarted",
            severity="annoyance",
            needs="a documented (or first-class) 'continue campaign on fork' — e.g. "
            "fork_campaign(campaign_id, at_commit) returning the rebound campaign",
            evidence="sable_court.py stage5 start_campaign(adopt) after fork_branch",
        )
        print(" · on the fork, the Spymaster sues for peace instead:")
        await self.play_beat(fork_engine, fork_campaign, script.FORK_BEATS[0][0])
        await self.commit_authored(
            fork_campaign,
            [
                edge_removed(src="f:vaelric", rel_type="at_war_with", dst="f:corvane"),
                edge_removed(src="f:corvane", rel_type="at_war_with", dst="f:vaelric"),
                thread_state_changed(thread_id="t:war-corvane-vaelric", to_state="resolved"),
                thread_state_changed(thread_id="t:vaelric-corvane-feud", to_state="resolved"),
            ],
            react_with=fork_engine,
        )
        fork_ledger.make_peace("vaelric", "corvane")
        await self.play_beat(fork_engine, fork_campaign, script.FORK_BEATS[1][0])
        # divergent downtime: the peace holds long enough for the SCHEDULED border war (r5a)
        await self.run_tick(
            40,
            fork_engine,
            fork_campaign,
            fork_ledger,
            mill=[("t:peace-of-the-fords", "A peace no one loves must be kept.")],
            battles_plan={},
        )
        await self.run_tick(
            40,
            fork_engine,
            fork_campaign,
            fork_ledger,
            mill=[("t:march-storm-plot", "Storm clouds over the march: Vaelric eyes Oldkeep.")],
            battles_plan={self.battle_no + 1: "march-storm"},
        )
        fork_day = await self.store.current_world_time(fork.branch_id)
        naive_rumor = [
            c.statement
            for c in await self.store.list_claims(fork.branch_id)
            if "walls will not hold" in c.statement
        ]
        check(
            "AGENDA (r5a, umbrella scope): the scheduled border war ignited past day 90",
            ("f:dellmoor", "f:vaelric") in await self.war_edges(fork.branch_id),
        )
        check(
            "the naive twin (r5b, single-faction scope) was SILENTLY dropped by the scope fence",
            len(naive_rumor) == 0,
        )
        frictionlog.gap(
            gap="A cross-House rule (declare war between two factions) under an honest scope",
            happened="the gauntlet requires BOTH edge endpoints inside ONE faction's "
            "jurisdiction; scoped to f:vaelric the action is dropped with no error, no log a "
            "pack author can see (r5b proves it — its rumor simply never exists)",
            workaround="an umbrella faction f:court that every House is member_of, purely to "
            "grant realm-wide rules jurisdiction (r5a) — a modeling hack",
            severity="major",
            needs="a 'world' scope (explicitly whole-realm jurisdiction) and drop diagnostics "
            "(a projected module-audit trail of dropped actions)",
            evidence="uro_core/engines/rules_gauntlet.py:88 (both-ends check); realm.py r5a vs r5b",
        )
        gareth_fork = await self.store.get_actor(fork.branch_id, "a:ser-gareth")
        gareth_main = await self.store.get_actor(self.branch, "a:ser-gareth")
        check(
            "the fork's war has its own dead: Ser Gareth fell on the fork, lives on main",
            gareth_fork is not None
            and gareth_fork.status == "dead"
            and gareth_main is not None
            and gareth_main.status == "alive",
        )

        # ── the diff: two histories from one log ──
        print("\n ── the divergence ──")
        main_day = await self.store.current_world_time(self.branch)
        main_wars = await self.war_edges(self.branch)
        fork_wars = await self.war_edges(fork.branch_id)
        main_feud = await self.thread_state(self.branch, "t:vaelric-corvane-feud")
        fork_feud = await self.thread_state(fork.branch_id, "t:vaelric-corvane-feud")
        main_rumors = await self.module_rumors(self.branch)
        fork_rumors = await self.module_rumors(fork.branch_id)
        print(f"   {'':24} MAIN (day {main_day})            FORK (day {fork_day})")
        print(f"   {'wars':24} {main_wars!r:32} {fork_wars!r}")
        print(f"   {'the feud thread':24} {main_feud:32} {fork_feud}")
        rumor_cell = f"{len(main_rumors)} distinct"
        print(f"   {'module rumors':24} {rumor_cell:32} {len(fork_rumors)} distinct")
        for r in sorted(set(main_rumors) - set(fork_rumors)):
            print(f"     main only: “{r}”")
        for r in sorted(set(fork_rumors) - set(main_rumors)):
            print(f"     fork only: “{r}”")
        check("the two lines disagree on a war edge", set(main_wars) != set(fork_wars))
        check("…and on a thread state", main_feud != fork_feud)
        check("…and on the rumor set", set(main_rumors) != set(fork_rumors))
        # replay audit: fork each head; a clean replay must reproduce the line's state exactly
        for label, branch in (("main", self.branch), ("fork", fork.branch_id)):
            info = await self.store.get_branch(branch)
            assert info is not None and info.head_commit is not None
            audit = await self.store.fork_branch(self.world_id, info.head_commit, f"audit-{label}")
            same_threads = {
                (t.thread_id, t.state) for t in await self.store.list_threads(branch)
            } == {(t.thread_id, t.state) for t in await self.store.list_threads(audit.branch_id)}
            same_wars = await self.war_edges(branch) == await self.war_edges(audit.branch_id)
            check(
                f"{label} replays cleanly (re-materialized state matches)",
                (same_threads and same_wars),
            )

        # OQ-8: the cascade the Reaction Layer could not do (computed in game code instead)
        frictionlog.refusal(
            name="Transitive alliance cascade (allies dragged into a declared war)",
            wished_rule="""{ "id": "the-web-of-oaths",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "at_war_with"}},
  "then": [{"do": "for_each", "traverse": "allied_with", "from": "$trigger.src", "as": "ALLY",
            "do": [{"do": "add_edge", "src": "ALLY", "rel": "at_war_with",
                    "dst": "$trigger.dst"},
                   {"do": "increment_counter", "counter": "tension(ALLY, $trigger.dst)",
                    "by": 2}]}],
  "scope": {"faction": "f:court"} }""",
            missing="graph traversal + trigger-payload binding ($trigger.src) + iteration — "
            "rules see only fixed, literal entity ids; every multi-hop consequence (ally of "
            "an ally goes wary, dependent plots wake) was computed in game code",
            where="sable_court.py sync_wars_from_uro / ledger.py (the cascade the game hand-rolls)",
        )
        frictionlog.refusal(
            name="Reactions that cascade (a rule triggered by a module event)",
            wished_rule="""{ "id": "counterplot-breeds-whispers",
  "trigger": {"event": "ThreadCreated", "where": {"provenance": "module"}},
  "then": [{"do": "record_rumor", "text": "Something moves against the new pact.",
            "subjects": ["a:maren-argent"]}],
  "scope": {"faction": "f:argent"} }""",
            missing="module events never re-enter react() (single-hop by design, no cascade "
            "budget) — r4a's counterplot can never itself breed consequences",
            where="uro_core/pipeline/engine.py:339 ('module events do not re-trigger it')",
        )

    def stage6_refusals(self) -> None:
        banner("6", "THE REFUSAL PASS — every realm rule the grammar could not say")
        frictionlog.print_refusal_log()
        check("the refusal log holds ≥ 8 concrete wished-for rules", len(frictionlog.REFUSALS) >= 8)

    def stage7_gaps(self) -> None:
        banner("7", "THE FRICTION LOG — raw material of GAP_REPORT.md")
        frictionlog.print_gap_table()
        print("\n   assembled + verdicts in examples/games/sable-court/GAP_REPORT.md")
        print("\nThe Sable Court concludes. The King lives — or so they say.")


async def _main() -> None:
    global STRICT
    parser = argparse.ArgumentParser(description="The Sable Court — a game built on Uro")
    parser.add_argument("--dsn", default=DSN)
    parser.add_argument(
        "--provider",
        default="scripted",
        choices=["scripted", "openai", "anthropic", "local"],
        help="scripted = deterministic, no key (default). Anything else narrates live "
        "(needs a key) and relaxes assertions to observations.",
    )
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    router = None
    if args.provider != "scripted":
        STRICT = False
        from uro_cli.wiring import build_router

        router = build_router(args.provider, args.model)
        print(f"[live mode: provider={args.provider}; assertions relaxed to observations]")
    store = PostgresEventStore(args.dsn)
    await store.connect()
    try:
        await store.migrate()
        game = SableCourt(store, router)
        print("THE SABLE COURT — the realm of Karsis, run on the Uro engine (Posture A)")
        await game.run()
    finally:
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
