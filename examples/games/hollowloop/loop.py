"""The loop engine: one origin, many branches. This is the meteor test as a game.

Every loop is a REAL `fork_branch(world_id, "origin", "loop-NNNN")` — the world resets by
construction (copy-on-write of the origin's projections), the Loopwalker's Codex does not. The
Fall is a committed `PlaceDestroyed` on that loop's branch. Break the loop and it is a committed
aversion instead, and the Vale is still standing when the branch ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import script
from codex import CODEX_BRANCH, Codex
from frictionlog import gap, refusal, timed
from script import Beat, ScriptedProvider
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    claim_recorded,
    item_transferred,
    place_destroyed,
    terrain_changed,
    thread_state_changed,
)
from uro_core.pipeline.engine import Engine
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign
from world import (
    CLUE_BY_STATEMENT,
    CLUES,
    DOOM,
    DOOM_SEGMENT,
    DOOM_STATES,
    DOOM_WORDS,
    KEYSTONES,
    NPC_NAMES,
    ORIGIN_REF,
    PARTICIPANT,
    PC_ID,
    PLACE_NAMES,
    RULE_PACK,
    SEGMENTS,
    TOWER_KEY,
    VALE,
    VISITABLE,
    genesis_events,
    who_is_at,
)

START_PLACE = "p:square"


# --------------------------------------------------------------------------------------------
# Bootstrap: the one world, the one origin marker every loop forks from
# --------------------------------------------------------------------------------------------


@dataclass
class Vale:
    store: PostgresEventStore
    engine: Engine
    provider: ScriptedProvider
    world_id: str
    main_branch: str
    origin_commit: str
    out_dir: Path


async def bootstrap(store: PostgresEventStore, out_dir: Path, name: str) -> Vale:
    """Create the world, bind the Loopwalker, and mark the ORIGIN — the fixed commit every loop
    forks from for the rest of the game's life."""
    provider = ScriptedProvider()
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))  # no ruleset, no key

    world = await store.create_world(
        name,
        tone=["elegiac", "hushed", "doomed"],
        rule_pack=RULE_PACK,
        extra_events=genesis_events(),
    )
    # The PC is bound ONCE, at origin, with a pinned id — every loop adopts this same actor.
    await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id=PARTICIPANT,
        new_pc_name="the Loopwalker",
        new_pc_id=PC_ID,
    )
    # THE ORIGIN. `create_marker` also writes a snapshot at that commit (store.py:912 — "markers
    # are the guaranteed snapshot points a fork can root from"), which is exactly why forking
    # hundreds of loops from it replays ZERO events: materialization is a pure snapshot restore.
    marker = await store.create_marker(world.world_id, ORIGIN_REF, world.main_branch_id)

    # The Codex's never-forked ledger branch (the BranchCodex backend lives here).
    await store.fork_branch(world.world_id, ORIGIN_REF, CODEX_BRANCH)

    _log_bootstrap_gaps()
    return Vale(
        store=store,
        engine=engine,
        provider=provider,
        world_id=world.world_id,
        main_branch=world.main_branch_id,
        origin_commit=marker.commit_id,
        out_dir=out_dir,
    )


def _log_bootstrap_gaps() -> None:
    gap(
        gap="A scripted provider keyed by the player's INTENT, so the same intent replays "
        "identically in any loop (the brief's own design)",
        happened="Not implementable: the extractor never sees the intent. "
        "`build_extractor_messages` sends only KNOWN ACTORS / KNOWN CLAIMS / NARRATION "
        "(pipeline/extraction.py:92-112) — deliberate player-text isolation, i.e. an "
        "anti-prompt-injection fence (extraction.py:10-12). So `complete(stage_tag='extractor')` "
        "cannot know which intent it is extracting for; only the narrator's `stream()` sees it",
        workaround="The game ARMS the provider with the chosen (narration, extraction) pair "
        "before each beat (script.py ScriptedProvider.arm). Deterministic and replayable, but "
        "the provider is now stateful and the game must never run two beats concurrently",
        severity="annoyance",
        needs="either a documented note that scripted providers must be armed/queued (the "
        "isolation is correct and should stay), or a beat-scoped correlation id on "
        "CompletionRequest so a provider can key its stages together",
        evidence="script.py ScriptedProvider.arm; pipeline/extraction.py:92-112",
    )
    gap(
        gap="Author the doom ladder in the fiction's own words "
        "(looming -> gathering -> imminent -> warded)",
        happened="REJECTED by the Reaction-Layer grammar: ThreadState is the closed literal "
        "['dormant','offered','active','resolved','dead'] (domain/events.py:797, pinned at "
        "worldpack/rules.py:45,114). Worse, an invalid pack does NOT raise — Engine.react and "
        "agenda_tick swallow the ValidationError into a logger.warning (engine.py:388-389, "
        "420-421) and the ENTIRE rule pack silently goes dark, so a one-word typo turns off "
        "every reaction in the world with no error anywhere",
        workaround="The ladder is punned onto the five words the grammar speaks "
        "(world.py DOOM_STATES) and DOOM_WORDS translates back for the UI; the pack is "
        "validated eagerly at import with RulePack(**RULE_PACK) so a typo fails loud",
        severity="major",
        needs="pack-declared thread-state vocabularies (validate against the pack, not a global "
        "Literal) — and a LOUD failure when a rule pack does not validate",
        evidence="world.py DOOM_STATES; game.py _validate_pack; domain/events.py:797; "
        "pipeline/engine.py:388-389",
    )
    refusal(
        name="escalate the dread after the player's THIRD fruitless visit",
        wished_rule="""{
  "id": "the-vale-notices-you",
  "trigger": {"event": "BeatResolved"},
  "when": {"kind": "visit_count", "place": "p:tower", "op": ">=", "value": 3},
  "then": [{"do": "set_thread_state", "thread": "t:doom", "to": "gathering"}],
  "scope": {"thread": "t:doom"}
}""",
        missing="counters / accumulating state. Conditions are compare-only over "
        "thread_state/actor_tier/actor_is_pc/edge_exists/world_day (worldpack/rules.py:95-105); "
        "nothing can count anything. A time-loop game's most natural rule — 'the Nth time you "
        "do X' — is inexpressible, and across loops it is doubly so (a fork resets the world, "
        "so even a hypothetical counter would reset with it).",
        where="loop.py — the game counts nothing; dread escalates purely on world_day, which is "
        "the only monotone quantity the grammar can read.",
    )
    refusal(
        name="the Fall itself, as a reaction to the hour",
        wished_rule="""{
  "id": "the-star-falls",
  "every_days": 1,
  "when": {"kind": "world_day", "op": ">=", "value": 6},
  "then": [{"do": "destroy_place", "place": "p:vale", "cause": "the Fall"}],
  "scope": {"place": "p:vale"}
}""",
        missing="any world-changing action. The Action union is a deliberate trust fence "
        "(worldpack/rules.py:156-164): set_thread_state / create_thread / record_rumor / "
        "spread_belief / add_edge / remove_edge. It structurally CANNOT destroy a place, move "
        "an item, or assert canon — which is correct for untrusted pack authors, and means the "
        "single most important event in this game can never be declarative.",
        where="loop.py commit_the_fall — host-authored place_destroyed via store.append_beat.",
    )


# --------------------------------------------------------------------------------------------
# A loop = a branch
# --------------------------------------------------------------------------------------------


@dataclass
class Loop:
    vale: Vale
    name: str
    branch_id: str
    campaign: Campaign
    segment: int = 0
    place: str = START_PLACE
    holds_key: bool = False
    outcome: str = "in progress"
    last_commit: str = ""
    discovered: list[str] = field(default_factory=list)
    beats: int = 0

    @property
    def segment_name(self) -> str:
        return SEGMENTS[min(self.segment, DOOM_SEGMENT)]


async def begin_loop(vale: Vale, n: int, *, from_ref: str = ORIGIN_REF, name: str = "") -> Loop:
    """Wake at dawn: fork a pristine day from the origin marker and adopt the Loopwalker on it."""
    loop_name = name or f"loop-{n:04d}"
    with timed("fork_branch"):
        branch = await vale.store.fork_branch(vale.world_id, from_ref, loop_name)
    # A fork copies proj_pcs, so the Loopwalker is ALREADY an active PC on the new branch
    # (is_pc -> True). But the `campaigns` ROW still points at the origin branch, and every
    # campaign-keyed store call (pc_for_participant, campaign_pcs, bind_pc, end_campaign) reads
    # the branch from that row — never from the Campaign object you hold. So a campaign must be
    # STARTED on the fork (the sanctioned pattern; tests/test_meteor.py:112-116 does exactly
    # this), adopting the same actor. See the gap below.
    with timed("start_campaign(fork)"):
        campaign = await vale.store.start_campaign(
            vale.world_id,
            branch.branch_id,
            participant_id=PARTICIPANT,
            adopt_actor_id=PC_ID,
        )
    _log_rebind_gap()
    return Loop(vale=vale, name=loop_name, branch_id=branch.branch_id, campaign=campaign)


def _log_rebind_gap() -> None:
    gap(
        gap="Re-point the existing campaign at the loop's forked branch "
        "(the brief's own words: 'rebind the campaign onto the forked branch')",
        happened="There is NO rebind. `campaigns.branch_id` is written once at INSERT and never "
        "updated (no `UPDATE campaigns` exists anywhere in the engine), and every campaign-keyed "
        "read resolves the branch from that ROW, not from the Campaign object passed in: "
        "`pc_for_participant`/`campaign_pcs` join `proj_pcs` to `campaigns` ON c.branch_id "
        "(store.py:641-670). So `campaign.model_copy(update={'branch_id': fork})` DOES run beats "
        "on the fork — but the engine resolves the acting PC against the campaign's ORIGINAL "
        "branch and only gets the right answer because the fork is a copy and the actor id is "
        "identical. It is correct by coincidence, and it breaks the moment the PC differs "
        "between the branches (or end_campaign releases it — then every later loop silently runs "
        "PC-less)",
        workaround="start_campaign(fork_branch, adopt_actor_id='a:pc') per loop — the sanctioned "
        "pattern (tests/test_meteor.py:112-116). Costs one commit + one campaigns row per loop, "
        "and leaves a stale copied proj_pcs row (from the origin campaign) on every fork",
        severity="major",
        needs="either `store.rebind_campaign(campaign_id, branch_id)`, or resolve the acting PC "
        "from the branch the beat is actually running on (the Campaign object's branch_id) "
        "instead of the campaigns row",
        evidence="loop.py begin_loop -> start_campaign per fork; "
        "adapters/postgres/store.py:641-670 (the join on c.branch_id); "
        "tests/test_hollowloop.py::test_the_model_copy_rebind_is_correct_by_coincidence",
    )


# --------------------------------------------------------------------------------------------
# Acting: one intent = one Uro beat = one segment of the doomed day
# --------------------------------------------------------------------------------------------


async def act(loop: Loop, beat: Beat, *, tick: bool = True) -> str:
    """Play one beat on this loop's branch, then advance the loop clock one segment."""
    vale = loop.vale
    vale.provider.arm(beat)
    with timed("run_beat"):
        result = await vale.engine.run_beat(loop.campaign, PARTICIPANT, beat.intent)
    loop.last_commit = result.commit_id
    loop.beats += 1
    await _harvest_clues(loop)
    if tick:
        await advance(loop)
    return result.narration


async def advance(loop: Loop) -> None:
    """One segment. `agenda_tick` = time_skip + the downtime agendas (rising dread).

    NB `store.time_skip` alone runs NO rules (store.py:577-607 commits TimeAdvanced +
    AdaptationApplied and nothing else), and `Engine.react` is never called on a time_skip commit
    — so a `Rule` triggering on `TimeAdvanced` can never fire. Day-driven logic MUST be an
    agenda, and agendas only fire through `Engine.agenda_tick`.
    """
    if loop.segment >= DOOM_SEGMENT:
        return
    with timed("agenda_tick"):
        await loop.vale.engine.agenda_tick(loop.branch_id, 1)
    loop.segment += 1
    gap(
        gap="A clock finer than a day (the loop is ONE day in seven segments)",
        happened="`world_day` is day-granular — `current_world_time(branch) -> int` and "
        "`time_skip(branch, days)` take whole days. `WorldTime` HAS a `segment` field "
        "(domain/events.py:23-27) but `time_advanced` never sets it and nothing reads it. There "
        "is no game<->world time mapping either: a beat costs no time at all (BeatPlan.time_cost "
        "is parsed and never consumed), so time only moves when the game says so",
        workaround="The seven segments of the doomed day ARE seven `world_day`s — the game "
        "borrows whole days as intra-day segments (world.py SEGMENTS). It works only because "
        "every loop forks from a day-0 origin, so absolute world_day == segment, which is the "
        "only reason the rule pack's ABSOLUTE `world_day` conditions can express 'as the day "
        "wears on'",
        severity="major",
        needs="a sub-day clock (populate and read WorldTime.segment), or a campaign-declared "
        "clock policy mapping beats to fiction-time",
        evidence="loop.py advance -> engine.agenda_tick(branch, 1) per segment; "
        "world.py SEGMENTS; domain/events.py:23-27 (segment field, unused)",
    )


async def _harvest_clues(loop: Loop) -> None:
    """Did this beat's extractor commit a keystone claim? Then the Loopwalker knows it forever.

    The clue's identity is its STATEMENT PROSE, because the extractor mints the claim id itself
    (`c:{ulid}`, extraction.py:185) and `ProposedClaim` has no id field — a game cannot ask Uro
    "is c:nature on this branch?", only "is there a claim whose text is exactly this?".
    """
    with timed("list_claims"):
        claims = await loop.vale.store.list_claims(loop.branch_id)
    for claim in claims:
        key = CLUE_BY_STATEMENT.get(claim.statement)
        if key and key not in loop.discovered:
            loop.discovered.append(key)
    gap(
        gap="Ask a branch whether it holds clue K1 (a stable, game-chosen claim key)",
        happened="The extractor MINTS the claim id (`claim_id = f'c:{new_id()}'`, "
        "extraction.py:185) and `ProposedClaim` has no id field (extraction.py:60-68) — a game "
        "cannot tag an extracted fact with its own key, and the minted ulid differs every run, "
        "so it is not even stable across replays of the same script",
        workaround="Clue identity is the exact statement PROSE: the game keeps "
        "CLUE_BY_STATEMENT and string-matches every claim on the branch (loop.py _harvest_clues). "
        "Ironically the game CAN choose ids for claims it authors itself via append_beat — so "
        "the host-authored Codex has stable `k:K1` ids while the extracted loop claims do not",
        severity="major",
        needs="an optional caller-supplied key on ProposedClaim (or an `extra` dict the gauntlet "
        "passes through to the ClaimRecorded payload)",
        evidence="loop.py _harvest_clues (string-matching CLUE_BY_STATEMENT); "
        "pipeline/extraction.py:60-68,185; codex.py BranchCodex.record (stable k: ids by "
        "contrast)",
    )


# --------------------------------------------------------------------------------------------
# What the Loopwalker may do here, now, knowing what they know
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    key: str  # a stable command key (e.g. "talk:a:aldis", "go:p:chapel")
    label: str
    beat: Beat
    clue: str | None = None  # the keystone this beat would discover, if any


def clue_here(loop: Loop, npc: str, codex: Codex) -> str | None:
    """Which keystone (if any) this NPC yields at this place+segment, given what we know."""
    for key in KEYSTONES:
        if key in loop.discovered:
            continue  # already learned THIS loop — the villager has nothing new
        clue = CLUES[key]
        if clue["about"] != [NPC_NAMES[npc]]:
            continue
        if any(req not in codex.known() for req in clue["requires"]):
            continue  # the Loopwalker doesn't yet know enough to ask the right question
        if key == "K1" and loop.segment <= 2 and loop.place == "p:chapel":
            return key
        if key == "K2" and loop.place == "p:chapel":
            return key
        if key == "K3" and loop.place == "p:well" and 3 <= loop.segment <= 4:
            return key
        if key == "K4" and loop.place == "p:tower" and 3 <= loop.segment <= 6:
            return key
    return None


def options(loop: Loop, codex: Codex) -> list[Option]:
    """The menu: movement + whoever the schedule puts here + Codex-gated intents."""
    if loop.segment >= DOOM_SEGMENT:
        opts: list[Option] = []
        if can_ring(loop, codex):
            opts.append(Option(key="ring", label="RING THE SKY-BELL", beat=script.RING))
        return opts

    opts = []
    for npc in who_is_at(loop.place, loop.segment):
        key = clue_here(loop, npc, codex)
        label = f"talk to {NPC_NAMES[npc]}"
        if key:
            label = f"ask {NPC_NAMES[npc]} about the Fall  [{CLUES[key]['title']}]"
        opts.append(
            Option(key=f"talk:{npc}", label=label, beat=script.talk_beat(npc, key), clue=key)
        )
    # the Codex-gated intent: you only search the well once Wren has told you (in ANY loop)
    if loop.place == "p:well" and "K3" in codex.known() and not loop.holds_key:
        opts.append(
            Option(key="search", label="search the well for the tower key", beat=script.SEARCH_WELL)
        )
    for place in VISITABLE:
        if place != loop.place:
            opts.append(
                Option(
                    key=f"go:{place}",
                    label=f"go to {PLACE_NAMES[place]}",
                    beat=script.go_beat(place, loop.segment_name),
                )
            )
    opts.append(
        Option(
            key="wait", label="wait, and watch the sky", beat=script.wait_beat(loop.segment_name)
        )
    )
    return opts


def can_ring(loop: Loop, codex: Codex) -> bool:
    return (
        codex.complete()
        and loop.holds_key
        and loop.place == "p:tower"
        and loop.segment >= DOOM_SEGMENT
    )


async def choose(loop: Loop, option: Option, codex: Codex) -> str:
    """Play an option: the beat, then any host-authored consequence it implies."""
    if option.key == "ring":
        return await ring_the_bell(loop, codex)
    narration = await act(loop, option.beat)
    if option.key.startswith("go:"):
        loop.place = option.key.removeprefix("go:")
    elif option.key == "search":
        await take_the_key(loop)
    if option.clue:
        await codex.record(option.clue, loop=loop.name, segment=max(0, loop.segment - 1))
    return narration


async def take_the_key(loop: Loop) -> None:
    """The key leaves Wren's hands and enters the Loopwalker's — a real ItemTransferred."""
    gap(
        gap="A beat that picks something up commits the ownership change",
        happened="A free-roam beat CANNOT commit an ItemTransferred: the extractor's whole "
        "vocabulary is actors+claims (pipeline/extraction.py:71-73), and the Reaction Layer's "
        "action union structurally cannot move an item either. So the narration says the key is "
        "in your coat while `items_owned_by` still says Wren has it",
        workaround="Host-authored `item_transferred` via store.append_beat immediately after the "
        "beat (+ engine.react by hand, because append_beat runs no rules)",
        severity="major",
        needs="a sanctioned effect channel for a beat to move an item (the ruleset's opaque "
        "effect path already does this for encounter loot — free-roam has no equivalent)",
        evidence="loop.py take_the_key -> store.append_beat([item_transferred(...)]); "
        "pipeline/extraction.py:71-73",
    )
    await _author(
        loop,
        [
            item_transferred(
                item_id=TOWER_KEY, from_ref="a:wren", to_ref=PC_ID, means="lifted from the well"
            )
        ],
    )
    loop.holds_key = True


async def _author(loop: Loop, events: list[Any]) -> str:
    """append_beat + the MANDATORY manual react (append_beat runs NO pack rules — only
    Engine._finish and the server's outcome path call react)."""
    with timed("append_beat"):
        commit = await loop.vale.store.append_beat(loop.branch_id, events)
    await loop.vale.engine.react(loop.campaign, commit.commit_id, events)
    loop.last_commit = commit.commit_id
    gap(
        gap="Host-authored events run the Reaction Layer the way played beats do",
        happened="`store.append_beat` commits but fires NO pack rules; only `Engine._finish` "
        "(run_beat) and the server's Chronicler path call `engine.react`. An embedder who "
        "forgets the manual call gets threads that silently never advance",
        workaround="loop.py _author wraps every append_beat with engine.react(campaign, "
        "commit_id, events)",
        severity="annoyance",
        needs="a store-level post-commit hook, or a documented `engine.append_and_react`",
        evidence="loop.py _author; pipeline/engine.py:339 (react is only called from _finish)",
    )
    return commit.commit_id


# --------------------------------------------------------------------------------------------
# The two endings, both committed to this loop's branch
# --------------------------------------------------------------------------------------------


async def commit_the_fall(loop: Loop) -> str:
    """Nightfall. The star arrives. This is the meteor, for real, on this loop's branch."""
    loop.vale.provider.arm(script.FALL)
    with timed("run_beat"):
        result = await loop.vale.engine.run_beat(loop.campaign, PARTICIPANT, script.FALL.intent)
    loop.last_commit = result.commit_id
    loop.beats += 1

    # The Fall is HOST-AUTHORED: the Reaction Layer cannot destroy a place (trust fence) and the
    # extractor can only propose actors+claims. `place_destroyed` + `append_beat` is the only
    # path — the same one tests/test_meteor.py:82-84 uses.
    #
    # The claim is not decoration: `PlaceDestroyed` flips proj_places.status and NOTHING else —
    # place state never reaches the narrator prompt (recall.py has no places field at all), so
    # without a claim the destruction of the Vale would be invisible to the prose forever. It
    # doubles as K4: witnessing a full loop to the Fall teaches you the hour.
    facts = script.fall_narration_facts()
    await _author(
        loop,
        [
            terrain_changed(
                place_id=VALE,
                description="a glassed crater where the Vale of Mourn stood",
                effects=["starfall"],
            ),
            place_destroyed(place_id=VALE, cause="the Fall"),
            thread_state_changed(
                thread_id=DOOM,
                to_state=DOOM_STATES["fallen"],
                from_state=DOOM_STATES["imminent"],
            ),
            claim_recorded(
                claim_id=CLUES["K4"]["id"],  # a stable id — because the GAME authored this one
                statement=facts["statement"],
                subject_refs=["a:harrow", VALE],
                truth="true",
                origin="narration",
            ),
        ],
    )
    gap(
        gap="The destruction of the Vale reaches the narrator's prose",
        happened="`PlaceDestroyed` flips `proj_places.status='destroyed'` and nothing else. "
        "Place state is NOT assembled into the narrator prompt at all — RecallBundle has no "
        "places field (pipeline/recall.py:26-39) — so on the next beat the GM has no idea the "
        "village it is describing is a crater",
        workaround="Commit a `claim_recorded` alongside the destruction (the same trick "
        "tests/test_meteor.py uses) so the fact enters the narrator's ESTABLISHED FACTS",
        severity="major",
        needs="place state (status/description) in the recall bundle and the narrator prompt",
        evidence="loop.py commit_the_fall; pipeline/recall.py:26-39 (no places); "
        "adapters/postgres/projector.py:124-129 (status flip only)",
    )
    loop.discovered.append("K4") if "K4" not in loop.discovered else None
    loop.outcome = f"fell @ seg {loop.segment}"
    return result.narration


async def ring_the_bell(loop: Loop, codex: Codex) -> str:
    """The win: the aversion, committed. The Vale is still standing when this branch ends."""
    loop.vale.provider.arm(script.RING)
    with timed("run_beat"):
        result = await loop.vale.engine.run_beat(loop.campaign, PARTICIPANT, script.RING.intent)
    loop.last_commit = result.commit_id
    loop.beats += 1
    await _author(
        loop,
        [
            # NB `thread_state_changed` has no `cause`/`why` field (domain/events.py:913-921) —
            # the single most important state change in the game commits with no reason attached;
            # the WHY has to live in the claim below.
            thread_state_changed(
                thread_id=DOOM,
                to_state=DOOM_STATES["warded"],
                from_state=DOOM_STATES["imminent"],
            ),
            claim_recorded(
                claim_id="c:aversion",
                statement=(
                    "The Loopwalker rang the Sky-Bell at the instant of the Fall, and the Vale "
                    "of Mourn survived the night."
                ),
                subject_refs=[PC_ID, VALE],
                truth="true",
                origin="narration",
            ),
        ],
    )
    await loop.vale.store.create_marker(loop.vale.world_id, "m:broke-the-loop", loop.branch_id)
    loop.outcome = "WARDED — the loop is broken"
    return result.narration


# --------------------------------------------------------------------------------------------
# Reading a loop back out of Uro (never out of game memory)
# --------------------------------------------------------------------------------------------


async def read_loop(store: PostgresEventStore, branch_id: str) -> dict[str, Any]:
    """Everything the UI shows about a branch, read from its head. Four queries per branch —
    this is the cross-branch fan-out that target 4 is about."""
    with timed("read_loop(4 queries)"):
        day = await store.current_world_time(branch_id)
        vale = await store.get_place(branch_id, VALE)
        threads = {t.thread_id: t.state for t in await store.list_threads(branch_id)}
        claims = await store.list_claims(branch_id)
    doom = threads.get(DOOM, DOOM_STATES["looming"])
    clues = sorted(
        {CLUE_BY_STATEMENT[c.statement] for c in claims if c.statement in CLUE_BY_STATEMENT}
    )
    status = (
        (vale or {}).get("status", "?") if isinstance(vale, dict) else getattr(vale, "status", "?")
    )
    return {
        "segment": day,
        "vale": status,
        "doom": DOOM_WORDS.get(doom, doom),
        "clues": clues,
        "claims": len(claims),
    }


async def loop_tree(store: PostgresEventStore, world_id: str) -> list[dict[str, Any]]:
    """The fork tree, reconstructed PURELY from Uro (no game-side branch registry).

    `list_branches` gives (name, head_commit, forked_from, head_depth) — enough to draw the tree.
    But the OUTCOME of each loop (did the Vale fall? which clues were found?) is only in each
    branch's projections, so it costs a fan-out of N x 4 queries. There is no aggregate/
    cross-branch query API at all.
    """
    with timed("list_branches"):
        branches = await store.list_branches(world_id)
    rows: list[dict[str, Any]] = []
    for b in sorted(branches, key=lambda x: x.name):
        state = await read_loop(store, b.branch_id)
        rows.append(
            {
                "name": b.name,
                "branch_id": b.branch_id,
                "forked_from": b.forked_from,
                "depth": b.head_depth,
                **state,
            }
        )
    gap(
        gap="Compare loops: 'which loop found which clue, when did each Vale fall, what "
        "endings happened' — one query across N branches",
        happened="There is NO cross-branch or aggregate query surface. `list_branches(world_id)` "
        "returns branch rows only; every projection read is `WHERE branch_id = $1`. Rendering "
        "the loop tree is therefore a client-side fan-out of N x 4 round-trips "
        "(current_world_time + get_place + list_threads + list_claims per loop), and "
        "current_world_time is itself a recursive CTE to genesis (store.py:748-765)",
        workaround="loop.py loop_tree does the N-branch fan-out by hand and the game times it "
        "(see the scale table's `loop_tree` row — it is the single slowest thing in the game)",
        severity="major",
        needs="a cross-branch read API: `query_across(branch_ids, projection) -> rows` and a "
        "`diff_branches(a, b) -> {added, removed, changed}` (see GAP_REPORT target 4 for the "
        "exact shape this game needed)",
        evidence="loop.py loop_tree; timings label 'read_loop(4 queries)' x N in the scale table",
    )
    return rows
