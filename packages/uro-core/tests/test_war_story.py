"""Phase 5 inc 5.4 — the WAR-STORY acceptance test (docs/10, D-25). Deterministic — no LLM.

An external toy battle in which the PC's spectacular feat has surviving enemy witnesses →
Chronicler distillation commits the feat + propagates it to the witnesses → beats later a tavern
NPC retells a distorted version, the belief chain traceable back to those witnesses. Re-run with
ZERO survivors and nobody ever mentions it.
"""

import sys
from collections.abc import AsyncIterator
from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import distill_outcome
from uro_core.domain.events import actor_created, edge_added
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from toy_battler import fight


async def _world_with_rumor_mill(store: PostgresEventStore) -> str:
    """PC + warband + a rumor path: raider1 →knows→ townsfolk →knows→ Mera (the tavern keeper)."""
    world = await store.create_world(f"warstory-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:hero", name="Sable the wizard", tier=2),
            actor_created(actor_id="a:champion", name="The warband champion", tier=2),
            actor_created(actor_id="a:raider1", name="A scarred raider", tier=1),
            actor_created(actor_id="a:raider2", name="A young raider", tier=1),
            actor_created(actor_id="a:townsfolk", name="A road pedlar", tier=1),
            actor_created(actor_id="a:mera", name="Mera", tier=1, role="tavern keeper"),
            edge_added(src="a:raider1", rel_type="knows", dst="a:townsfolk"),
            edge_added(src="a:townsfolk", rel_type="knows", dst="a:mera"),
        ],
    )
    return branch


class _SpyNarrator:
    """Records the narrator's context so we can see whether Mera's rumor reaches the prompt."""

    def __init__(self) -> None:
        self.context = ""

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        self.context = "\n".join(m.content for m in req.messages)
        yield "Mera leans in and, half-believing, repeats what she heard."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _trace(store: PostgresEventStore, branch: str, actor: str, claim_id: str) -> list[str]:
    chain = [actor]
    while True:
        belief = next(
            (b for b in await store.beliefs_of(branch, chain[-1]) if b.claim_id == claim_id), None
        )
        if belief is None or belief.learned_from is None:
            break
        chain.append(belief.learned_from)
    return chain


async def test_war_story_feat_becomes_a_traceable_rumor(store: PostgresEventStore) -> None:
    branch = await _world_with_rumor_mill(store)

    # the EXTERNAL game resolves the battle; one raider survives to witness it
    outcome = fight("a:hero", ["a:champion", "a:raider1", "a:raider2"], seed=7, survivors=1)
    assert outcome.witnesses == ["a:raider1"]
    await store.append_beat(branch, await distill_outcome(store, branch, outcome))

    # the feat is on record as the external game's TESTIMONY (truth=unknown, not Uro's canon) —
    # an external bundle cannot assert protected canon; its witnesses merely believe it
    feat = next(c for c in await store.claims_about(branch, "a:hero") if "champion" in c.statement)
    assert feat.truth == "unknown" and feat.origin == "external"

    # Mera (T1, two hops from the witness, never at the battle) believes a GARBLED version...
    mera = next(b for b in await store.beliefs_of(branch, "a:mera") if b.claim_id == feat.claim_id)
    assert mera.confidence < 0.45  # third-hand → a low-confidence rumor, not eyewitness certainty
    # ...traceable back through the contact graph to the surviving witness
    assert await _trace(store, branch, "a:mera", feat.claim_id) == [
        "a:mera",
        "a:townsfolk",
        "a:raider1",
    ]

    # ...and beats later she RETELLS it — with the DISTORTION (low confidence) reaching the
    # narrator: the rumor surfaces framed as a rumor, not settled fact
    spy = _SpyNarrator()
    engine = Engine(store, ProviderRouter(bindings={}, default=spy))
    campaign = await store.start_campaign(
        (await store.get_branch(branch)).world_id,
        branch,
        participant_id="p1",
        new_pc_name="Traveler",
        new_pc_id="a:traveler",
    )
    await engine.run_beat(campaign, "p1", "I ask Mera what she has heard of late")
    assert feat.statement in spy.context  # the feat rumor reached the narrator via Mera's belief
    assert "has heard a rumor" in spy.context  # ...and reached it as a RUMOR (confidence surfaced)


async def test_war_story_no_survivors_no_rumor(store: PostgresEventStore) -> None:
    branch = await _world_with_rumor_mill(store)

    outcome = fight("a:hero", ["a:champion", "a:raider1", "a:raider2"], seed=7, survivors=0)
    assert outcome.witnesses == []
    await store.append_beat(branch, await distill_outcome(store, branch, outcome))

    # the feat is still on record (an unwitnessed testimony claim exists)...
    assert any("champion" in c.statement for c in await store.claims_about(branch, "a:hero"))
    # ...but with no survivors, nobody ever heard of it: Mera holds no belief about it
    assert await store.beliefs_of(branch, "a:mera") == []
