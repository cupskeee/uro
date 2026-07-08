"""Beat pipeline (docs/05, 10, 13).

context → [plan → mechanics] → narrate → extract → gauntlet → commit. Phase 1's epistemic
loop (recall → narrate → extract) stands; Phase 3 (D-28) inserts the planner + mechanics gate
when a ruleset is bound: the planner classifies intent and picks affordances, plan validation
fences it (D-21), and the ruleset resolves the checks deterministically. With no ruleset
bound (Phase 0/1 compat, `--bare`) the planner/gate are skipped and the flow is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    CausedBy,
    DomainEvent,
    beat_resolved,
    claim_recorded,
    item_transferred,
    mode_changed,
    sheet_updated,
)
from uro_core.domain.ids import new_id
from uro_core.errors import (
    EmptyNarrationError,
    PlannerError,
    ProviderError,
    UnboundParticipantError,
)
from uro_core.metering import LLMCall
from uro_core.pipeline.encounter import run_encounter
from uro_core.pipeline.extraction import (
    build_extractor_messages,
    parse_extraction,
    run_gauntlet,
)
from uro_core.pipeline.mechanics import encounter_trigger, resolve_mechanics
from uro_core.pipeline.plan import (
    BeatPlan,
    PlanMechanic,
    build_planner_messages,
    parse_plan,
    validate_plan,
)
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle, assemble_recall, build_narrator_messages
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import (
    CharSpec,
    CheckResult,
    Combatant,
    EncounterOutcome,
    Ruleset,
)
from uro_core.rulesets.rng import Rng
from uro_core.timeline.models import Campaign

logger = logging.getLogger(__name__)


class BeatResult(BaseModel):
    beat_id: str
    narration: str
    commit_id: str
    extracted: int = 0  # number of state events canonicalized from the prose
    checks: int = 0  # ruleset checks resolved this beat (planner→mechanics gate)
    suggestions: list[str] = Field(default_factory=list)  # affordance-grounded hints (D-23)


class _Context(BaseModel):
    """What context assembly produced for the narrator + commit (docs/13 BeatState subset)."""

    recall: RecallBundle
    plan: BeatPlan | None = None
    mechanics_traces: list[str] = Field(default_factory=list)
    directives: str = ""
    suggestions: list[str] = Field(default_factory=list)
    # The PC the ACTING participant drives this beat (OQ-7) — resolved once from participant_id and
    # reused by planning, the mechanics gate, and the encounter aggressor. "" = no bound PC.
    pc_actor_id: str = ""


def _hash_messages(messages: list[Message]) -> str:
    payload = json.dumps([m.model_dump() for m in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Engine:
    """Embeddable engine entry point. Wired with concrete adapters by the CLI/server."""

    def __init__(
        self,
        store: EngineStore,
        router: ProviderRouter,
        *,
        ruleset: Ruleset | None = None,
        recency: int = 8,
        semantic_k: int = 4,
        bare: bool = False,
    ) -> None:
        self._store = store
        self._router = router
        # The bound ruleset enables the planner + mechanics gate (D-28). None → Phase-1 flow.
        self._ruleset = ruleset
        self._recency = recency
        self._semantic_k = semantic_k
        # bare = ablation baseline (thesis T1): a raw-transcript GM — no structured/
        # semantic recall, no extraction, no memory. Just the narrator over recent beats.
        self._bare = bare
        # Per-branch prompt style (tone) + template env, from the world's pack (docs/09). Cached.
        self._style_cache: dict[str, tuple[str, PromptEnv]] = {}

    @property
    def ruleset_id(self) -> str:
        """The id of the bound ruleset ('' if none). Lets a caller (e.g. the server) detect a
        campaign pinned to a DIFFERENT ruleset than this Engine holds, and reject gracefully
        instead of crashing deep in sheet validation (D-30 per-campaign binding)."""
        return self._ruleset.id if self._ruleset is not None else ""

    async def _prompt_style(self, branch_id: str) -> tuple[str, PromptEnv]:
        """The world's narrator style + prompt-pack env for a branch (cached). A world created
        without a pack yields ('', DEFAULT_ENV) — the shipped default templates, no tone."""
        if branch_id not in self._style_cache:
            style, overrides = await self._store.world_style(branch_id)
            env = PromptEnv(overrides) if overrides else DEFAULT_ENV
            self._style_cache[branch_id] = (style, env)
        return self._style_cache[branch_id]

    async def _recall(self, branch_id: str, intent_text: str) -> RecallBundle:
        """Structured recall + semantic recall of older beats (docs/04).

        Structured recall always stands; semantic recall is best-effort aux — an embed
        or vector-search failure (provider down, a mismatched embedder dimension on the
        branch) degrades to structured-only, never crashes the beat.
        """
        if self._bare:  # ablation: transcript only, no state
            recent = await self._store.recent_beats(branch_id, self._recency)
            return RecallBundle(recent_beats=recent, actors=[], claims=[], beliefs=[])
        recall = await assemble_recall(self._store, branch_id, intent_text, self._recency)
        recent_texts = {b.narration for b in recall.recent_beats}
        started = time.perf_counter()
        try:
            vectors = await self._router.embed("embedder", [intent_text])
            await self._meter("embedder", [Message(role="user", content=intent_text)], started)
            vector = vectors[0] if vectors else None
            if not vector or not any(
                vector
            ):  # empty response or zero-norm (no words) → recall nothing
                return recall
            hits = await self._store.search(branch_id, vector, self._semantic_k)
        except Exception as exc:  # best-effort; structured recall stands
            logger.warning("semantic recall failed, using structured recall only: %s", exc)
            return recall
        # Drop recency-window overlaps and byte-identical dupes; semantic recall is for OLD beats.
        seen: set[str] = set()
        for hit in hits:
            if hit.text in recent_texts or hit.text in seen:
                continue
            seen.add(hit.text)
            recall.memories.append(hit.text)
        return recall

    async def run_beat(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> BeatResult:
        """Resolve one beat and commit it (narration + extracted state, or a resolved fight)."""
        ctx = await self._context(campaign, participant_id, intent_text)
        messages, encounter_events = await self._prepare_narration(campaign, ctx, intent_text)
        started = time.perf_counter()
        chunks = [chunk async for chunk in self._router.stream("narrator", messages)]
        await self._meter("narrator", messages, started)
        narration = "".join(chunks).strip()
        return await self._finish(
            campaign, participant_id, intent_text, narration, ctx, encounter_events
        )

    async def run_beat_stream(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> AsyncIterator[str]:
        """Stream narration to the caller, then extract + commit once the stream ends.

        A beat commits only after the stream completes. If the consumer stops early
        (e.g. Ctrl-C mid-stream) the commit is intentionally skipped: nothing partial
        enters the append-only log, so a resumed session simply never saw that beat.
        """
        ctx = await self._context(campaign, participant_id, intent_text)
        messages, encounter_events = await self._prepare_narration(campaign, ctx, intent_text)
        started = time.perf_counter()
        collected: list[str] = []
        async for chunk in self._router.stream("narrator", messages):
            collected.append(chunk)
            yield chunk
        await self._meter("narrator", messages, started)
        await self._finish(
            campaign, participant_id, intent_text, "".join(collected).strip(), ctx, encounter_events
        )

    async def _prepare_narration(
        self, campaign: Campaign, ctx: _Context, intent_text: str
    ) -> tuple[list[Message], list[DomainEvent] | None]:
        """Decide free-roam vs combat and build the narrator prompt. When the plan invokes an
        encounter-starting affordance (attack), resolve the whole fight deterministically FIRST
        (no LLM), then narrate its outcome — returning the fight's events for the commit stage."""
        style, env = await self._prompt_style(campaign.branch_id)
        trigger = (
            encounter_trigger(self._ruleset, ctx.plan)
            if (self._ruleset is not None and ctx.plan is not None)
            else None
        )
        if trigger is not None:
            resolved = await self._resolve_encounter(campaign, ctx, trigger)
            if resolved is not None:  # a valid fight formed; otherwise fall through to free-roam
                events, traces = resolved
                messages = build_narrator_messages(
                    ctx.recall,
                    intent_text,
                    pc_actor_id=ctx.pc_actor_id,
                    mechanics_traces=traces,
                    directives="A fight breaks out — narrate it, honoring these outcomes.",
                    style=style,
                    env=env,
                )
                return messages, events
        messages = build_narrator_messages(
            ctx.recall,
            intent_text,
            pc_actor_id=ctx.pc_actor_id,
            mechanics_traces=ctx.mechanics_traces,
            directives=ctx.directives,
            style=style,
            env=env,
        )
        return messages, None

    async def _context(self, campaign: Campaign, participant_id: str, intent_text: str) -> _Context:
        """Context assembly [1] + (when a ruleset is bound) plan [2] + mechanics gate [3].
        The plan/gate run before any prose streams, so a re-ask replan is still free (docs/13).
        The acting PC is resolved from the SUBMITTING participant (OQ-7 party play): a beat by
        participant P is planned/gated as P's PC — falling back to the campaign's solo PC when P
        has no binding (single-player, or an unseated observer)."""
        recall = await self._recall(campaign.branch_id, intent_text)
        pc_actor_id = await self._acting_pc(campaign, participant_id)
        if self._ruleset is None or self._bare:
            return _Context(recall=recall, pc_actor_id=pc_actor_id)
        plan = await self._plan(campaign, intent_text, recall, pc_actor_id)
        checks = await self._mechanics(campaign, plan, recall, pc_actor_id)
        return _Context(
            recall=recall,
            plan=plan,
            mechanics_traces=[c.trace for c in checks],
            directives=plan.narration_directives,
            suggestions=plan.suggestions,
            pc_actor_id=pc_actor_id,
        )

    async def _acting_pc(self, campaign: Campaign, participant_id: str) -> str:
        """The PC the submitting participant drives (OQ-7). If the participant is bound, that's it.
        If unbound, fall back to the campaign's PC ONLY when the campaign is SOLO (exactly one PC)
        — in a PARTY an unbound participant must NOT silently drive another player's PC (cross-phase
        review P7xP2/P3); the beat is refused so they must `uro campaign join` first."""
        pc = await self._store.pc_for_participant(campaign.campaign_id, participant_id)
        if pc:
            return pc
        # Unbound participant. Decide by how many PCs the campaign has:
        pcs = await self._store.campaign_pcs(campaign.campaign_id)
        if len(pcs) == 1:
            return pcs[0]  # SOLO fallback — the one PC is safe to act as
        if len(pcs) >= 2:  # PARTY — refuse rather than silently drive another player's PC
            raise UnboundParticipantError(
                f"participant {participant_id!r} has no PC in this party campaign — "
                f"run `uro campaign join` to bind one before taking a turn"
            )
        return ""  # no PC bound at all (a Phase-1 / narration-only campaign) — act with no PC

    async def _resolve_ref(self, branch_id: str, ref: str) -> str:
        """A plan actor/target ref → a KNOWN actor id, or "" if unresolvable. A known id passes
        through; a NAME the planner emitted ("Cass") is entity-resolved via find_actor_by_name
        (which canonicalizes internally, like the extractor/Chronicler). Never mints an actor."""
        if not ref:
            return ""
        if await self._store.get_actor(branch_id, ref) is not None:
            return ref
        match = await self._store.find_actor_by_name(branch_id, ref)
        return match.actor_id if match is not None else ""

    async def preview_beat(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> list[DomainEvent]:
        """Dry-run a beat (docs/09 creator loop): run the full pipeline — plan, narrate, extract —
        but DO NOT commit. Returns the would-be events for inspection (the event diff). Nothing
        enters the append-only log, so the campaign state is untouched."""
        ctx = await self._context(campaign, participant_id, intent_text)
        messages, encounter_events = await self._prepare_narration(campaign, ctx, intent_text)
        started = time.perf_counter()
        chunks = [chunk async for chunk in self._router.stream("narrator", messages)]
        await self._meter("narrator", messages, started)
        narration = "".join(chunks).strip()
        events, _, _ = await self._beat_events(
            campaign, participant_id, intent_text, narration, ctx, encounter_events
        )
        return events

    async def _finish(
        self,
        campaign: Campaign,
        participant_id: str,
        intent_text: str,
        narration: str,
        ctx: _Context,
        encounter_events: list[DomainEvent] | None = None,
    ) -> BeatResult:
        events, extracted_n, beat_id = await self._beat_events(
            campaign, participant_id, intent_text, narration, ctx, encounter_events
        )
        commit = await self._store.append_beat(campaign.branch_id, events)
        if not self._bare:
            await self._remember(
                campaign.branch_id,
                commit.commit_id,
                narration,
                [a.actor_id for a in ctx.recall.actors],
            )
        return BeatResult(
            beat_id=beat_id,
            narration=narration,
            commit_id=commit.commit_id,
            extracted=extracted_n,
            checks=len(ctx.mechanics_traces),
            suggestions=ctx.suggestions,
        )

    async def _beat_events(
        self,
        campaign: Campaign,
        participant_id: str,
        intent_text: str,
        narration: str,
        ctx: _Context,
        encounter_events: list[DomainEvent] | None,
    ) -> tuple[list[DomainEvent], int, str]:
        """Build a beat's committable events (shared by run and dry-run). Runs the extractor for
        a free-roam beat; wraps a resolved fight for a combat beat. Does NOT commit."""
        if not narration:
            raise EmptyNarrationError(
                f"provider produced no narration for a beat by {participant_id}"
            )
        beat_id = new_id()
        if encounter_events is not None:
            # Combat beat: the ruleset's mechanical events ARE the state (no extraction). Wrap
            # them with ModeChanged in/out and the narrating BeatResolved, all one commit.
            cause = CausedBy(kind="player_action", participant_id=participant_id, beat_id=beat_id)
            events: list[DomainEvent] = [
                mode_changed(
                    from_mode="freeroam", to_mode="encounter", cause=intent_text, caused_by=cause
                ),
                *encounter_events,
                mode_changed(
                    from_mode="encounter",
                    to_mode="freeroam",
                    cause="fight resolved",
                    caused_by=cause,
                ),
                beat_resolved(
                    beat_id=beat_id,
                    participant_id=participant_id,
                    intent_text=intent_text,
                    narration=narration,
                ),
            ]
            extracted_n = 0
        else:
            # Free-roam: canonicalize prose through the extractor gauntlet. Bare mode records
            # only the transcript.
            extracted = (
                [] if self._bare else await self._extract(campaign.branch_id, ctx.recall, narration)
            )
            events = [
                beat_resolved(
                    beat_id=beat_id,
                    participant_id=participant_id,
                    intent_text=intent_text,
                    narration=narration,
                ),
                *extracted,
            ]
            extracted_n = len(extracted)
        return events, extracted_n, beat_id

    async def _resolve_encounter(
        self, campaign: Campaign, ctx: _Context, trigger: PlanMechanic
    ) -> tuple[list[DomainEvent], list[str]] | None:
        """Build combatants from sheets (a default for an unsheeted combatant), auto-resolve the
        fight (docs/06, no LLM), and derive its consequences. Returns (events, traces), or None
        when no real fight forms — an attack with no distinct, known opponent falls back to
        free-roam rather than fabricating a won encounter against no one (review 3.3)."""
        assert self._ruleset is not None
        branch = campaign.branch_id
        # The acting participant's PC (OQ-7) — so P3's attack loots/injures on P3's PC, not the
        # campaign's first PC. Resolved once in _context, reused here.
        pc_id = ctx.pc_actor_id
        # ENTITY-RESOLVE the plan's refs (live-run finding, 2026-07-09): a small planner routinely
        # names the target ("Cass") instead of emitting its id ("a:cass"), which used to fall
        # through get_actor and silently drop the fight to free-roam. Resolve name→id (reusing the
        # extractor/Chronicler resolver) so "seize by force from Cass" actually forms the encounter.
        aggressor = await self._resolve_ref(branch, trigger.actor or pc_id)
        defender = await self._resolve_ref(branch, trigger.target)
        # A real encounter needs two DISTINCT, KNOWN actors on OPPOSING sides. PC identity only
        # attributes consequences; it does not decide the split (so NPC-vs-NPC works too). An
        # unresolvable ref came back "" here → no fight forms (falls back to free-roam).
        if not aggressor or not defender or aggressor == defender:
            return None
        # No auto-resolved PvP (cross-phase review P7xP3): a party member's single beat must NOT
        # down + loot ANOTHER player's PC with no agency. If the target is another active PC, fall
        # back to free-roam (the clash is narrated, not mechanically auto-resolved). Consensual PvP
        # is future work behind the same seam.
        if await self._store.is_pc(branch, defender):
            return None

        setup: list[DomainEvent] = []
        combatants: list[Combatant] = []
        for actor_id, team in ((aggressor, "aggressor"), (defender, "defender")):
            if await self._store.get_actor(branch, actor_id) is None:
                return None  # a ref that is not a known actor → not a real fight
            sheet_dict = await self._store.get_sheet(branch, actor_id)
            if sheet_dict is None:  # a known-but-unsheeted combatant gets a default sheet, logged
                sheet_dict = self._ruleset.new_character(CharSpec(), Rng(0))
                setup.append(
                    sheet_updated(actor_id=actor_id, sheet=sheet_dict, ruleset_id=self._ruleset.id)
                )
            combatants.append(Combatant(actor_id=actor_id, team=team, sheet=sheet_dict))

        encounter_id = f"e:{new_id()}"
        rng = await self._beat_rng(campaign)
        enc_events, outcome = run_encounter(
            self._ruleset, combatants, rng, encounter_id=encounter_id
        )
        consequences = await self._combat_consequences(branch, outcome, combatants)

        traces = [
            e.payload["trace"]
            for e in enc_events
            if e.event_type == "EncounterTurnTaken" and e.payload.get("trace")
        ]
        traces.append(f"outcome: team {outcome.winner_team or 'none'} prevails")
        return [*setup, *enc_events, *consequences], traces

    async def _combat_consequences(
        self, branch_id: str, outcome: EncounterOutcome, combatants: list[Combatant]
    ) -> list[DomainEvent]:
        """Persistent fallout of a decided fight: each combatant on the losing team is wounded
        (a truth=true claim) and the victor loots their items (ItemTransferred). Emitted by P."""
        if outcome.winner_team is None:
            return []
        victors = [c.actor_id for c in combatants if c.team == outcome.winner_team]
        losers = [c.actor_id for c in combatants if c.team != outcome.winner_team]
        if not victors or not losers:
            return []
        victor = victors[0]
        cause = CausedBy(kind="player_action")
        events: list[DomainEvent] = []
        for loser in losers:
            # Use the display name in the claim's prose (recall feeds it to the narrator); keep
            # the actor id only in subject_refs, never leak a raw a:… id into narration.
            actor = await self._store.get_actor(branch_id, loser)
            name = actor.name if actor is not None else loser
            events.append(
                claim_recorded(
                    claim_id=f"c:{new_id()}",
                    statement=f"{name} was beaten down in the brawl and left wounded.",
                    subject_refs=[loser],
                    truth="true",
                    origin="mechanics",
                    caused_by=cause,
                )
            )
            for item_id in await self._store.items_owned_by(branch_id, loser):
                events.append(
                    item_transferred(
                        item_id=item_id,
                        from_ref=loser,
                        to_ref=victor,
                        means="looted",
                        caused_by=cause,
                    )
                )
        return events

    async def _plan(
        self, campaign: Campaign, intent_text: str, recall: RecallBundle, pc_actor_id: str
    ) -> BeatPlan:
        """Planner [2] + deterministic plan validation. The only replanning point (docs/13):
        up to 2 re-asks with the validation error attached; exhausting them fails the beat."""
        assert self._ruleset is not None
        affordances = self._ruleset.affordances()
        known_ids = {a.actor_id for a in recall.actors} | ({pc_actor_id} if pc_actor_id else set())
        _, env = await self._prompt_style(campaign.branch_id)
        messages = build_planner_messages(affordances, recall, pc_actor_id, intent_text, env=env)
        reason = "no plan produced"
        for _ in range(3):  # 1 attempt + 2 re-asks (docs/13)
            started = time.perf_counter()
            raw = await self._router.complete(
                "planner", messages, json_mode=True, temperature=0.2, max_tokens=1024
            )
            await self._meter("planner", messages, started)
            plan = parse_plan(raw)
            if plan is None:
                reason = "output was not a valid BeatPlan JSON object"
            else:
                errors = validate_plan(plan, affordances, known_ids)
                if not errors:
                    return plan
                reason = "; ".join(errors)
            messages = [
                *messages,
                Message(
                    role="user",
                    content=f"That plan was invalid ({reason}). Return a corrected JSON BeatPlan.",
                ),
            ]
        raise PlannerError(f"planner failed after re-asks: {reason}")

    async def _mechanics(
        self, campaign: Campaign, plan: BeatPlan, recall: RecallBundle, pc_actor_id: str
    ) -> list[CheckResult]:
        """Mechanics gate [3]: resolve the plan's free-roam checks via the ruleset. No LLM."""
        assert self._ruleset is not None
        actor_ids = {pc_actor_id} if pc_actor_id else set()
        actor_ids |= {m.actor for m in plan.mechanics if m.actor}
        sheets: dict[str, dict[str, Any]] = {}
        for aid in actor_ids:
            sheet = await self._store.get_sheet(campaign.branch_id, aid)
            if sheet is not None:
                sheets[aid] = sheet
        if pc_actor_id and pc_actor_id not in sheets:
            # A ruleset-bound beat whose PC has no sheet resolves 0 checks — surface it rather
            # than silently skip. (world new / campaign new sheet the PC; this is an edge.)
            logger.warning("ruleset-bound beat: PC %r has no sheet; checks skipped", pc_actor_id)
        rng = await self._beat_rng(campaign)
        return resolve_mechanics(self._ruleset, plan, sheets, pc_actor_id, rng)

    async def _beat_rng(self, campaign: Campaign) -> Rng:
        """A per-beat seeded Rng derived from the campaign + the commit it builds on — stable
        and reproducible for the same history, so a beat's rolls replay (docs/06, 10)."""
        branch = await self._store.get_branch(campaign.branch_id)
        head = (branch.head_commit if branch else None) or campaign.branch_id
        digest = hashlib.sha256(f"{campaign.campaign_id}:{head}".encode()).hexdigest()
        return Rng(int(digest[:12], 16))

    async def _remember(
        self, branch_id: str, commit_id: str, text: str, entity_refs: list[str]
    ) -> None:
        """Embed the beat's narration and index it for later semantic recall.

        Post-commit and fully best-effort: the beat is ALREADY committed, so nothing
        here — a failed embed, an empty vector, or a memory-write DB error — may raise
        out and fail a beat that persisted. The memory index is a rebuildable aux cache.
        """
        started = time.perf_counter()
        try:
            vectors = await self._router.embed("embedder", [text])
            await self._meter("embedder", [Message(role="user", content=text)], started)
            vector = vectors[0] if vectors else None
            if not vector or not any(vector):  # empty/zero-norm → nothing worth indexing
                return
            await self._store.add_memory(
                branch_id=branch_id,
                commit_id=commit_id,
                kind="beat",
                text=text,
                vector=vector,
                entity_refs=entity_refs,
            )
        except Exception as exc:  # never fail a committed beat
            logger.warning("memory write failed for a committed beat: %s", exc)

    async def _extract(
        self, branch_id: str, recall: RecallBundle, narration: str
    ) -> list[DomainEvent]:
        """Extract state from prose through the gauntlet. Failure → narration-only beat
        (docs/13: state integrity is never sacrificed to keep prose, and prose is never
        lost to keep state)."""
        _, env = await self._prompt_style(branch_id)
        messages = build_extractor_messages(recall, narration, env=env)
        started = time.perf_counter()
        raw: str | None = None
        try:
            # Generous cap: extraction JSON can be dense; a truncated response parses to
            # nothing and silently drops state (worse than a slow beat).
            raw = await self._router.complete(
                "extractor", messages, json_mode=True, temperature=0.1, max_tokens=4096
            )
        except ProviderError as exc:
            logger.warning("extractor call failed; committing narration-only beat: %s", exc)
            raw = None
        await self._meter("extractor", messages, started)  # meter even a failed call
        if raw is None:
            return []
        extraction = parse_extraction(raw)
        if extraction is None:
            logger.warning(
                "extractor output was not parseable JSON; committing narration-only beat"
            )
            return []
        return await run_gauntlet(self._store, branch_id, extraction)

    async def _meter(self, stage_tag: str, messages: list[Message], started: float) -> None:
        # Metering is best-effort observability — it must never break a beat.
        try:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._store.record_llm_call(
                LLMCall(
                    stage_tag=stage_tag, prompt_hash=_hash_messages(messages), latency_ms=latency_ms
                )
            )
        except Exception as exc:
            logger.warning("metering failed for stage %s: %s", stage_tag, exc)
