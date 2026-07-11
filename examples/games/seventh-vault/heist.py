"""The heist director — everything the GAME must resolve because Uro cannot (yet).

Three jobs, each a finding:

1. THE GUARD RESPONSE (Stage 6): at lockdown the game rolls its own seeded skirmish dice and
   reports the result as a Chronicler OutcomeBundle over POST /outcome — the designed external
   posture. Uro's trust model then does exactly what D-32 promises: the tier-0 guard dies, the
   tier-3 Warden's "death" is downgraded to truth=unknown testimony, the surviving witness
   carries the rumor, and the zero-survivor scuffle propagates silence.

2. HOST-AUTHORED CANON: taking the prize and the double-cross are free-roam intents, and a
   free-roam beat cannot commit an ItemTransferred (only encounter loot can, and the stub
   planner can start no encounter; even a live planner refuses PvP: engine.py:507). So the
   ownership changes the heist turns on are `store.append_beat` events authored HERE — and
   `append_beat` does not run reactions, so the director must call `engine.react` by hand or
   the score thread would never notice the prize moved.

3. WOUNDS: the OutcomeBundle vocabulary is casualties/feats/loot — there is no non-lethal harm,
   so Brakk's skirmish wounds land as a host-authored SheetUpdated.
"""

from __future__ import annotations

import asyncio
import json
import random
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from client import CrewClient
from frictionlog import gap
from host import HeistWorld, ServerHandle
from players import (
    HOOK_GALLERY,
    HOOK_GATE,
    HOOK_LOCKDOWN,
    HOOK_PRIZE,
    HOOK_THEFT,
    Pacer,
)
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import item_transferred, sheet_updated
from uro_core.pipeline.engine import Engine
from world import (
    ALARM_THREAD,
    ALARM_WORDS,
    CREW,
    GUARD_CELLAR,
    GUARD_FALLEN,
    GUARD_WITNESS,
    KEYRING,
    PRIZE,
    RULESET_ID,
    SCORE_THREAD,
    SCORE_WORDS,
    TAPSTER,
    WARDEN,
)

SKIRMISH_SEED = 7  # the game's OWN dice — Uro gives us no way to seed its own (see host.py gap)


# --------------------------------------------------------------------------------------------
# The skirmish: the external mini-game, resolved with our dice
# --------------------------------------------------------------------------------------------


@dataclass
class SkirmishResult:
    rounds: int
    brakk_wounds: int
    log: list[str] = field(default_factory=list)


def resolve_skirmish(seed: int = SKIRMISH_SEED) -> SkirmishResult:
    """The lockdown guard response, rolled outside Uro. Structure is fixed by the fiction
    (Ott presses the stair and falls; Reyla breaks and runs when he does; the Warden holds the
    door and takes a wound the crew will swear was mortal); the DICE decide how long it takes
    and how badly Brakk bleeds. Deterministic for a fixed seed — that determinism belongs to
    the game, not to Uro."""
    rng = random.Random(seed)
    ott_hp, wounds, rounds, log = 6, 0, 0, []
    while ott_hp > 0:
        rounds += 1
        crew_roll, guard_roll = rng.randint(1, 20), rng.randint(1, 20)
        if crew_roll + 3 >= 12:  # Brakk's press
            dealt = rng.randint(2, 8)
            ott_hp -= dealt
            log.append(f"round {rounds}: Brakk hits Ott for {dealt} (roll {crew_roll}+3)")
        else:
            log.append(f"round {rounds}: Brakk misses (roll {crew_roll}+3)")
        if guard_roll >= 10 and ott_hp > 0:
            hurt = rng.randint(1, 4)
            wounds += hurt
            log.append(f"round {rounds}: Ott's halberd bites Brakk for {hurt} (roll {guard_roll})")
    log.append(
        f"round {rounds}: Ott falls; Reyla breaks for the stairs; the Warden holds the "
        "door, bleeding"
    )
    return SkirmishResult(rounds=rounds, brakk_wounds=max(1, wounds), log=log)


def lockdown_bundle(result: SkirmishResult) -> dict[str, Any]:
    """The OutcomeBundle for the guard response. NOTE what the game TRIES to assert: the crew
    swears the Warden fell — Uro must refuse that canon (tier 3 -> testimony). And note what it
    CANNOT say at all: Brakk's wounds (no non-lethal field)."""
    return {
        "v": 1,
        "encounter_id": "e:lockdown-1",
        "participants": [*[c[2] for c in CREW], GUARD_FALLEN, GUARD_WITNESS, WARDEN],
        "witnesses": [GUARD_WITNESS],
        "casualties": [GUARD_FALLEN, WARDEN],  # the Warden claim is the D-32 downgrade probe
        "feats": [
            {
                "actor": "a:brakk",
                "description": "held the Security Hub stair alone and put down Guardsman Ott",
            }
        ],
        "loot": [{"item_id": KEYRING, "from_ref": GUARD_FALLEN, "to_ref": "a:brakk"}],
        "duration_rounds": result.rounds,
    }


def chute_scuffle_bundle() -> dict[str, Any]:
    """The betrayal ending's second encounter: Sable silences the cellar-watch on her way out.
    ZERO surviving witnesses -> the feat claim exists but propagates to nobody (the engine's
    'the world only remembers what someone lived to tell' contract)."""
    return {
        "v": 1,
        "encounter_id": "e:chute-scuffle",
        "participants": ["a:sable", GUARD_CELLAR],
        "witnesses": [],
        "casualties": [GUARD_CELLAR],
        "feats": [
            {
                "actor": "a:sable",
                "description": "went out the coal chute alone with the Heart, "
                "leaving her crew in the dark",
            }
        ],
        "loot": [],
        "duration_rounds": 1,
    }


async def post_outcome(server: ServerHandle, campaign_id: str, bundle: dict[str, Any]) -> dict:
    """POST the bundle over the wire (Posture B) — the one management-ish endpoint that exists."""
    url = (
        f"{server.base}/campaigns/{campaign_id}/encounters/{bundle['encounter_id']}"
        f"/outcome?token={CREW[0][1]}"
    )

    def _post() -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(bundle).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return dict(json.loads(resp.read()))

    result = await asyncio.to_thread(_post)
    gap(
        gap="Learn from the outcome POST what Uro actually accepted (which casualty stuck, "
        "which was downgraded, which loot was dropped)",
        happened=f"POST /outcome returns only {result} — no per-item verdicts; the protection "
        "downgrade and scope drops are silent",
        workaround="Diff library projections after the POST (get_actor status, claims_about) "
        "to discover what the trust gate did",
        severity="annoyance",
        needs="a verdict list in the outcome response ({casualties: [{ref, applied|downgraded|"
        "dropped, why}], ...})",
        evidence="heist.py post_outcome; uro_server/app.py:116-120; chronicler.py distill_outcome",
    )
    return result


# --------------------------------------------------------------------------------------------
# The director: watches the shared stream, lands canon between beats
# --------------------------------------------------------------------------------------------


class Director:
    def __init__(
        self,
        store: PostgresEventStore,
        engine: Engine,
        hw: HeistWorld,
        server: ServerHandle,
        ending: str,
    ) -> None:
        self.store = store
        self.engine = engine
        self.hw = hw
        self.server = server
        self.ending = ending
        self.skirmish: SkirmishResult | None = None

    async def _alarm_word(self) -> str:
        threads = {t.thread_id: t.state for t in await self.store.list_threads(self.hw.branch_id)}
        return ALARM_WORDS[threads[ALARM_THREAD]]

    async def _score_word(self) -> str:
        threads = {t.thread_id: t.state for t in await self.store.list_threads(self.hw.branch_id)}
        return SCORE_WORDS[threads[SCORE_THREAD]]

    async def _author(self, events: list[Any]) -> None:
        """append_beat + the MANDATORY manual react — the footgun logged once here."""
        commit = await self.store.append_beat(self.hw.branch_id, events)
        await self.engine.react(self.hw.campaign, commit.commit_id, events)
        gap(
            gap="Authored events trigger the Reaction Layer like played beats do",
            happened="store.append_beat commits but does NOT run pack rules — only "
            "Engine._finish and the server outcome path call react(); an embedder that "
            "forgets the manual call gets threads that silently never advance",
            workaround="Director._author wraps every append_beat with engine.react(campaign, "
            "commit_id, events)",
            severity="annoyance",
            needs="either store-level post-commit hooks or a documented engine.append_and_react",
            evidence="heist.py Director._author; pipeline/engine.py:339 (react only in _finish); "
            "uro_server/app.py:81",
        )

    # ---- hooks, keyed by 0-based beat index --------------------------------------------

    async def after_gate_blunder(self) -> None:
        word = await self._alarm_word()
        assert word == "suspicious", f"alarm after gate blunder: {word}"
        print(f"[director] alarm: calm -> {word} (rule alarm-1, off the committed beat)")

    async def after_gallery_blunder(self) -> None:
        word = await self._alarm_word()
        assert word == "alerted", f"alarm after gallery blunder: {word}"
        print(f"[director] alarm: suspicious -> {word} (rule alarm-2)")
        # the rule CANNOT also mark that the guards now know Brakk's face — multi-dimension
        # scope refusal (see rule_pack.register_refusals: 'spotted' entry)

    async def on_lockdown(self) -> None:
        word = await self._alarm_word()
        assert word == "lockdown", f"alarm after hub blunder: {word}"
        print(f"[director] alarm: alerted -> {word} (rule alarm-3) — the House answers")
        self.skirmish = resolve_skirmish()
        for line in self.skirmish.log:
            print(f"[skirmish] {line}")
        await post_outcome(
            self.server, self.hw.campaign.campaign_id, lockdown_bundle(self.skirmish)
        )
        # read back what the trust gate did (no verdicts in the response — gap logged there)
        fallen = await self.store.get_actor(self.hw.branch_id, GUARD_FALLEN)
        warden = await self.store.get_actor(self.hw.branch_id, WARDEN)
        assert fallen and fallen.status == "dead", f"Ott should be canon-dead: {fallen}"
        assert warden and warden.status != "dead", f"the Warden must NOT be canon-dead: {warden}"
        warden_claims = [
            c.statement
            for c in await self.store.claims_about(self.hw.branch_id, WARDEN)
            if c.truth == "unknown" and "fallen" in c.statement
        ]
        assert warden_claims, "the Warden's 'death' should exist as truth=unknown testimony"
        print(
            f"[director] D-32 held: Ott dead; the Warden only 'said to have fallen' "
            f"({len(warden_claims)} testimony claim)"
        )
        keyring = await self.store.get_item(self.hw.branch_id, KEYRING)
        assert keyring and keyring.get("owner_ref") == "a:brakk", keyring
        # Brakk's wounds: the bundle had nowhere to put them -> author the sheet
        gap(
            gap="Report non-lethal harm in the OutcomeBundle (Brakk limps out of the skirmish)",
            happened="The bundle vocabulary is casualties/feats/loot only "
            "(chronicler.py OutcomeBundle) — a wound either kills or never happened",
            workaround="Host-authored SheetUpdated with the game's own hp arithmetic "
            "(caused_by=system, the host-authorship tier — see the whitelist gap below)",
            severity="major",
            needs="an `injuries: [{actor, effect}]` bundle field distilled through the same "
            "protection ceiling (PC wounds as testimony? that needs design — which is the point)",
            evidence="heist.py on_lockdown; chronicler.py:64-78 (OutcomeBundle fields)",
        )
        # Provenance note: an earlier draft stamped this event caused_by=external_cause(...) —
        # the Chronicler E-tier — and append_beat took it UNCHALLENGED, even though the sanctioned
        # E-tier producer (distill_outcome) can never emit a SheetUpdated, least of all one
        # mutating a protected PC. That is live evidence for the engine's named
        # emitter-whitelist-at-append deferral; the event now carries the honest default
        # (kind="system", like the game's other host-authored canon).
        gap(
            gap="The append boundary refuses provenance the named trust tier could never mint",
            happened="store.append_beat accepted a SheetUpdated stamped "
            "caused_by=external_cause(...) — E-tier canon mutating a protected PC's sheet, "
            "which distill_outcome (the only sanctioned E-tier producer) is structurally unable "
            "to emit. No append-time emitter whitelist exists (a named engine deferral)",
            workaround="The game polices itself: host-authored events carry the default "
            "kind='system' cause",
            severity="annoyance",
            needs="the append-time emitter whitelist (caused_by.kind -> allowed event set), "
            "already named as a deferral in docs/12 — this is a live consumer tripping it",
            evidence="heist.py on_lockdown (this call site); uro_core/chronicler.py "
            "distill_outcome (the fixed E-tier event set)",
        )
        sheet = await self.store.get_sheet(self.hw.branch_id, "a:brakk")
        assert sheet is not None
        sheet = dict(sheet)
        sheet["hp"] = max(1, int(sheet["hp"]) - self.skirmish.brakk_wounds)
        await self._author([sheet_updated(actor_id="a:brakk", sheet=sheet, ruleset_id=RULESET_ID)])
        print(
            f"[director] Brakk took {self.skirmish.brakk_wounds} wounds -> hp {sheet['hp']} "
            "(host-authored SheetUpdated; the bundle had no field for it)"
        )

    async def on_prize_taken(self) -> None:
        gap(
            gap="'I lift the Heart from its cradle' commits the ownership change (the beat IS "
            "the take)",
            happened="A free-roam beat cannot commit ItemTransferred: the extractor whitelist "
            "is actors+claims (extraction.py:71), encounter loot is the only pipeline path "
            "(engine.py:568-576), and the stub planner can start no encounter — the narration "
            "says 'you take it' while the projection still says the vault owns it",
            workaround="The host authors item_transferred(i:prize, p:seventh-vault -> a:vesna) "
            "via append_beat the moment the beat commits, then react()s so the score thread "
            "notices; a pure WS client could not do this AT ALL",
            severity="blocker",
            needs="a ruleset 'take/transfer' affordance whose effect emits ItemTransferred "
            "through the mechanics gate (the D-30 opaque-effect path already exists for sheets)",
            evidence="heist.py on_prize_taken -> store.append_beat(item_transferred(...)); "
            "pipeline/extraction.py:71; pipeline/engine.py:568",
        )
        await self._author(
            [
                item_transferred(
                    item_id=PRIZE,
                    from_ref="p:seventh-vault",
                    to_ref="a:vesna",
                    means="lifted from its cradle",
                )
            ]
        )
        word = await self._score_word()
        assert word == "prize-taken", f"score after the take: {word}"
        prize = await self.store.get_item(self.hw.branch_id, PRIZE)
        assert prize and prize.get("owner_ref") == "a:vesna", prize
        print(
            "[director] the Heart is out of its cradle: score pending -> prize-taken "
            "(rule score-1 fired on the authored ItemTransferred)"
        )

    async def on_double_cross(self) -> None:
        gap(
            gap="A consensual-PvP double-cross resolved as mechanics (Sable lifts the prize "
            "off Vesna on her turn)",
            happened="No path exists: the stub planner emits no mechanics; a live planner's "
            "attack on a PC falls back to free-roam BY DESIGN (engine.py:507-508, the P7 "
            "anti-grief fix); uro-basic has no steal/take affordance; round-robin has no "
            "consent step to make it 'consensual'",
            workaround="The betrayal is a narration beat + a host-authored item_transferred "
            "(means='the double-cross') — the outcome is real committed state, but no rule of "
            "the game decided it; the host simply declared it",
            severity="major",
            needs="the consensual-PvP arbiter shape (S1): a contested-action protocol where "
            "the target's client consents (or contests with a check) before the effect commits",
            evidence="heist.py on_double_cross; pipeline/engine.py:507-508; "
            "stress/s1_arbiter.py PvP probe",
        )
        await self._author(
            [
                item_transferred(
                    item_id=PRIZE,
                    from_ref="a:vesna",
                    to_ref="a:sable",
                    means="the double-cross",
                )
            ]
        )
        prize = await self.store.get_item(self.hw.branch_id, PRIZE)
        assert prize and prize.get("owner_ref") == "a:sable", prize
        # score-1 must NOT re-fire (its `when` pins pending); the state pun survives the theft
        word = await self._score_word()
        assert word == "prize-taken", f"score after the theft: {word}"
        print(
            "[director] the double-cross: the Heart moved a:vesna -> a:sable "
            "(host-authored; no PvP mechanics exist — gap logged)"
        )
        # Sable silences the cellar watch: the zero-survivor Chronicler probe
        await post_outcome(self.server, self.hw.campaign.campaign_id, chute_scuffle_bundle())
        cellar = await self.store.get_actor(self.hw.branch_id, GUARD_CELLAR)
        assert cellar and cellar.status == "dead", cellar
        print(
            "[director] chute scuffle reported: Umble dead, zero witnesses — the betrayal "
            "feat must propagate to NOBODY (asserted in the finale)"
        )

    def hooks(self) -> dict[int, Callable[[], Awaitable[None]]]:
        table: dict[int, Callable[[], Awaitable[None]]] = {
            HOOK_GATE: self.after_gate_blunder,
            HOOK_GALLERY: self.after_gallery_blunder,
            HOOK_LOCKDOWN: self.on_lockdown,
            HOOK_PRIZE: self.on_prize_taken,
        }
        if self.ending == "betrayal":
            table[HOOK_THEFT] = self.on_double_cross
        return table

    async def run(
        self,
        observer: CrewClient,
        script: list[tuple[int, str]],
        pacer: Pacer,
    ) -> None:
        """Watch the shared stream; after each hooked beat commits, land the hook's canon while
        the pacer holds the table, then let the next thief move."""
        for beat_index, hook in sorted(self.hooks().items()):
            intent = script[beat_index][1]
            await observer.wait_for("beat_committed", where={"intent": intent}, timeout=120)
            await hook()
            pacer.release(beat_index + 1)


# --------------------------------------------------------------------------------------------
# The finale: read EVERYTHING back from committed state, assert the ending, build the digest
# --------------------------------------------------------------------------------------------


async def final_readout(
    store: PostgresEventStore, hw: HeistWorld, ending: str, skirmish: SkirmishResult
) -> dict[str, Any]:
    branch = hw.branch_id
    threads = {t.thread_id: t.state for t in await store.list_threads(branch)}
    alarm, score = ALARM_WORDS[threads[ALARM_THREAD]], SCORE_WORDS[threads[SCORE_THREAD]]
    prize = await store.get_item(branch, PRIZE)
    prize_owner = (prize or {}).get("owner_ref", "")
    statuses = {}
    for actor_id in (GUARD_FALLEN, GUARD_WITNESS, GUARD_CELLAR, WARDEN, *[c[2] for c in CREW]):
        a = await store.get_actor(branch, actor_id)
        statuses[actor_id] = a.status if a else "MISSING"
    warden_testimony = sorted(
        c.statement
        for c in await store.claims_about(branch, WARDEN)
        if c.truth == "unknown" and "fallen" in c.statement
    )
    feat_beliefs = {
        actor: sorted(
            (b.claim_id, round(b.confidence, 3)) for b in await store.beliefs_of(branch, actor)
        )
        for actor in (GUARD_WITNESS, TAPSTER, WARDEN)
    }
    betrayal_feat_believers = [
        actor
        for actor in (GUARD_WITNESS, TAPSTER, WARDEN)
        for claim_id, _c in feat_beliefs[actor]
        if "chute-scuffle" in claim_id
    ]
    legend_set: set[str] = set()
    for actor_id in (c[2] for c in CREW):
        for claim in await store.claims_about(branch, actor_id):
            if claim.origin == "module" and "They say" in claim.statement:
                legend_set.add(claim.statement)
    legend = sorted(legend_set)
    brakk_sheet = await store.get_sheet(branch, "a:brakk") or {}
    return {
        "ending": ending,
        "alarm": alarm,
        "score": score,
        "prize_owner": prize_owner,
        "statuses": statuses,
        "warden_testimony": warden_testimony,
        "feat_beliefs": feat_beliefs,
        "betrayal_feat_believers": betrayal_feat_believers,
        "legend": legend,
        "brakk_hp": brakk_sheet.get("hp"),
        "brakk_max_hp": brakk_sheet.get("max_hp"),
        "skirmish_rounds": skirmish.rounds,
        "keyring_owner": ((await store.get_item(branch, KEYRING)) or {}).get("owner_ref", ""),
    }


def assert_ending(r: dict[str, Any]) -> None:
    assert r["alarm"] == "lockdown", r["alarm"]
    assert r["statuses"][GUARD_FALLEN] == "dead", r["statuses"]
    assert r["statuses"][WARDEN] != "dead", r["statuses"]
    assert r["warden_testimony"], "no Warden testimony claim"
    assert r["feat_beliefs"][GUARD_WITNESS], "the surviving witness carries no rumor"
    assert r["feat_beliefs"][TAPSTER], "the rumor never reached the tapster (knows-hop)"
    witness_conf = dict(r["feat_beliefs"][GUARD_WITNESS])
    tapster_conf = dict(r["feat_beliefs"][TAPSTER])
    shared = set(witness_conf) & set(tapster_conf)
    assert shared and all(tapster_conf[c] < witness_conf[c] for c in shared), (
        "hop decay missing: tapster should believe the same claim less than the eyewitness"
    )
    if r["ending"] == "clean":
        assert r["score"] == "escaped", r["score"]
        assert r["prize_owner"] == "a:vesna", r["prize_owner"]
        assert any("crew of four" in s for s in r["legend"]), r["legend"]
        assert r["statuses"][GUARD_CELLAR] != "dead", "clean run must not touch the cellar watch"
    else:
        assert r["score"] == "betrayed", r["score"]
        assert r["prize_owner"] == "a:sable", r["prize_owner"]
        assert r["statuses"][GUARD_CELLAR] == "dead", r["statuses"]
        assert not r["betrayal_feat_believers"], (
            f"zero-survivor scuffle leaked a rumor to {r['betrayal_feat_believers']}"
        )
        assert any("Ghost" in s for s in r["legend"]), r["legend"]
    assert isinstance(r["brakk_hp"], int) and r["brakk_hp"] < int(r["brakk_max_hp"]), (
        r["brakk_hp"],
        r["brakk_max_hp"],
    )
    assert r["keyring_owner"] == "a:brakk", r["keyring_owner"]


def digest(r: dict[str, Any], beat_log: list[Any]) -> str:
    """One canonical string for byte-determinism comparison across runs (ids excluded — world/
    campaign/commit ids are fresh ulids per run BY DESIGN; content must not vary)."""
    beats = ";".join(f"{b.participant_id}~{b.intent}~{b.narration}" for b in beat_log)

    def _belief_cell(pairs: list[tuple[str, float]]) -> str:
        return ",".join(f"{cid.split(':', 2)[-1]}={conf}" for cid, conf in pairs)

    beliefs = ";".join(
        f"{actor}:{_belief_cell(pairs)}" for actor, pairs in sorted(r["feat_beliefs"].items())
    )
    return "|".join(
        [
            r["ending"],
            r["alarm"],
            r["score"],
            r["prize_owner"],
            ",".join(f"{k}={v}" for k, v in sorted(r["statuses"].items())),
            ";".join(r["warden_testimony"]),
            beliefs,
            ";".join(r["legend"]),
            f"brakk_hp={r['brakk_hp']}",
            f"rounds={r['skirmish_rounds']}",
            f"keyring={r['keyring_owner']}",
            beats,
        ]
    )
