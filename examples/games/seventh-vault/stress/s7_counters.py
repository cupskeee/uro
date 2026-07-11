"""S7 — the no-counters wall, measured three ways:

(a) the grammar refuses the heist's vocabulary and counters (captured pydantic errors), and
    ACCEPTS a trigger on an event that can never fire (the silent-inert footgun);
(b) even when a check IS forced through the mechanics gate (Posture A, scripted planner), its
    d20 outcome commits NOTHING — the Reaction Layer could never read it anyway;
(c) the roll cannot be reproduced across runs — the beat RNG seeds off the fresh-per-run
    campaign_id, so mechanical outcomes are replay-deterministic but never RUN-deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402
import re  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import host  # noqa: E402
import pydantic  # noqa: E402
from common import Evidence  # noqa: E402
from frictionlog import gap  # noqa: E402
from rule_pack import register_refusals  # noqa: E402
from world import crew_sheet  # noqa: E402


# --- (b)'s instrument: a provider whose PLANNER always orders one sneak check, and which
# KEEPS ITS OWN NARRATOR PROMPTS — because the prompt is the only artifact anywhere that
# carries the resolved roll (BeatResult.checks is just an int COUNT, engine.py:78; the
# CheckResult objects never leave the pipeline). The game scrapes its own GM prompt to learn
# its own dice. ---
class ForcedCheckProvider:
    def __init__(self) -> None:
        self.pc_id = ""  # set after the campaign exists
        self.narrator_prompts: list[str] = []

    async def stream(self, req: Any) -> AsyncIterator[str]:
        self.narrator_prompts.append("\n".join(str(m.content) for m in req.messages))
        yield "[scripted] The shadow work happens; the dice already spoke."

    async def complete(self, req: Any) -> str:
        if req.stage_tag == "planner":
            return (
                '{"intent_class": "action", "triggers": ["stealth"], '
                f'"mechanics": [{{"affordance": "sneak", "actor": "{self.pc_id}"}}]}}'
            )
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from uro_core.providers.adapters.stub import hashing_embedding

        return [hashing_embedding(t) for t in texts]


async def grammar_probes(ev: Evidence) -> None:
    from uro_core.worldpack.rules import RulePack

    try:
        RulePack(
            rules_api_version=1,
            rules=[
                {
                    "id": "r",
                    "trigger": {"event": "BeatResolved"},
                    "then": [{"do": "set_thread_state", "thread": "t:alarm", "to": "suspicious"}],
                    "scope": {"thread": "t:alarm"},
                }
            ],
        )
        ev.log("UNEXPECTED: custom thread state accepted")
    except pydantic.ValidationError as e:
        line = next(ln.strip() for ln in str(e).splitlines() if "Input should be" in ln)
        ev.log(f"(a) `to: 'suspicious'` REFUSED: {line}")
    pack = RulePack(
        rules_api_version=1,
        rules=[
            {
                "id": "r",
                "trigger": {"event": "CheckResolved", "where": {"outcome": "failure"}},
                "then": [{"do": "set_thread_state", "thread": "t:alarm", "to": "dead"}],
                "scope": {"thread": "t:alarm"},
            }
        ],
    )
    ev.log(
        f"(a) trigger on {pack.rules[0].trigger.event!r} ACCEPTED — but no such event type "
        "exists anywhere in domain/events.py; the rule validates and is silently inert "
        "forever (the accepted-but-dead footgun)"
    )


async def forced_check_probe(ev: Evidence) -> tuple[list[int], list[int]]:
    """Run the same 3-beat sneak sequence in TWO fresh worlds; return both roll sequences."""
    from uro_core.pipeline.engine import Engine
    from uro_core.providers.router import ProviderRouter
    from uro_core.rulesets import registry

    sequences: list[list[int]] = []
    store = await host.connect_store()
    try:
        for run in (1, 2):
            provider = ForcedCheckProvider()
            engine = Engine(
                store,
                ProviderRouter(bindings={}, default=provider),
                ruleset=registry.resolve("uro-basic", ""),
            )
            world = await store.create_world(
                f"s7-probe-{run}", ruleset_id="uro-basic", extra_events=[]
            )
            campaign = await store.start_campaign(
                world.world_id,
                world.main_branch_id,
                participant_id="player-1",
                new_pc_name="Probe",
                pc_sheet=crew_sheet({"DEX": 14}),
                ruleset_id="uro-basic",
                seed=99,  # identical stored seed both runs — watch it not matter
            )
            branch = world.main_branch_id
            pc = await store.campaign_pc(campaign.campaign_id)
            assert pc is not None
            provider.pc_id = pc
            sheet_before = dict(await store.get_sheet(branch, pc) or {})
            rolls: list[int] = []
            for i in range(3):
                result = await engine.run_beat(campaign, "player-1", f"I sneak past post {i}.")
                assert result.checks == 1, "the scripted planner must force exactly one check"
                trace = re.search(r"check: d20 \((\d+)\)[^\n]*", provider.narrator_prompts[-1])
                assert trace, "the check trace must reach the narrator prompt"
                rolls.append(int(trace.group(1)))
                if run == 1 and i == 0:
                    ev.log(
                        f"(b) a REAL d20 resolved through the gate — but BeatResult.checks "
                        f"is only the COUNT ({result.checks}, engine.py:78); the outcome "
                        f"exists ONLY as a prompt string the game must scrape from its own "
                        f"narrator request: {trace.group(0)!r}"
                    )
            if run == 1:
                sheet_after = dict(await store.get_sheet(branch, pc) or {})
                claims = await store.list_claims(branch)
                threads = await store.list_threads(branch)
                ev.log(
                    f"(b) after 3 checked beats: sheet unchanged={sheet_before == sheet_after}, "
                    f"claims={len(claims)}, thread-changes={len(threads)} — the rolls left "
                    "ZERO committed trace (no event type carries a check outcome; "
                    "CheckResult.trace feeds only the narrator prompt)"
                )
            sequences.append(rolls)
    finally:
        await store.close()
    return sequences[0], sequences[1]


async def main() -> None:
    ev = Evidence("s7_counters")
    register_refusals()
    await grammar_probes(ev)
    run1, run2 = await forced_check_probe(ev)
    ev.log(
        f"(c) same intents, same sheet, same stored seed=99, two fresh runs: "
        f"rolls {run1} vs {run2}"
        + (
            " — DIFFERENT: the beat RNG is "
            "sha256(campaign_id:head_commit) (engine.py:632-638) and campaign_id is a fresh "
            "ulid; the stored CampaignStarted.seed is never read"
            if run1 != run2
            else " — equal by d20 coincidence this time; the seed derivation is still "
            "campaign_id-bound (engine.py:632-638)"
        )
    )
    gap(
        gap="Reaction rules that read CHECK OUTCOMES and COUNT them (the alarm as a heat meter)",
        happened="Triple wall, each independently fatal: (1) checks commit no event — the "
        "mechanics layer is invisible to the Reaction Layer; (2) the grammar has no counters/"
        "arithmetic; (3) the grammar's ThreadState is a closed 5-word Literal, so the alarm "
        "cannot even be NAMED calm/suspicious/alerted/lockdown",
        workaround="A 4-rule enum ladder punned onto dormant/offered/active/dead, each rung "
        "keyed to the EXACT intent_text of one scripted blunder via trigger.where — prose "
        "pattern-matching in place of mechanics",
        severity="major",
        needs="(1) an optional CheckResolved committed event (opt-in per ruleset); (2) counter "
        "state in the grammar (or the reserved WASM tier); (3) pack-declared thread-state "
        "vocabularies",
        evidence="stress/s7_counters.py (all three probes captured); worldpack/rules.py:38; "
        "pipeline/mechanics.py:29; pipeline/engine.py:632-638",
    )
    ev.log(
        "VERDICT: the enum-state workaround is doubly lossy — the meter's distance-to-"
        "lockdown is inexpressible, and the ladder rungs bind to prose strings that a "
        "one-word script edit silently disarms. Plus the scope wrinkle: an alarm rule that "
        "also wanted to mark the blunderer 'spotted' needs thread+actor in one scope and "
        "must be split (and the split still fails add_edge's both-endpoints rule). See the "
        "refusal log (RL entries) printed by the arc."
    )
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
