# Sable Court — Uro Gap Report

## 1. Summary

Uro's current surface supports a real court-intrigue game surprisingly well — the whole arc
(seeded realm of 18 actors / 7 factions / 7 places / 12→27 threads, 11 court beats, 5 downtime
ticks, Chronicler battles, and a fork whose two lines demonstrably diverge on wars, thread
states, rumors, and who is dead) runs deterministically, keyless, and byte-stable, with 46
printed assertions green. The single biggest wall is exactly the one the experiment was designed
to find: **the declarative Reaction Layer cannot express any of the realm's numeric simulation**
— no counters, no arithmetic, no accumulating state, no loops, no tables — so the entire House
economy/tension/war engine lives in game code as a "shadow ledger" (12 refusal-log entries, §5).
That shadow state then breaks the engine's signature feature from the outside: `fork_branch`
forks every Uro projection perfectly, but the game must manually snapshot/restore its own
numbers at the exact fork commit (G-10). The second wall is the trust ceiling colliding with the
genre: a game *about* assassinating great lords can never actually kill one (G-7) — every T2+
death, battlefield or bedchamber, is downgraded to a rumor, which is great flavor and a hard
cap on the fantasy.

## 2. Gap table

| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity (blocker\|major\|annoyance\|cosmetic) | What Uro would need (concrete engine change) | Evidence (call/file/line) |
|---|---|---|---|---|---|
| G-1: The realm sim (strength/gold/influence/tension per House) as world state Uro owns, forkable/replayable | The rule grammar has no counters, arithmetic beyond compares, accumulating state, loops, or weighted tables — the entire numeric realm lives in game code, invisible to forks/exports/rules | The shadow ledger (`ledger.py`), reflected qualitatively each tick; manually snapshotted at fork points | blocker | The reserved engine-owned computation tier (D-33 Stage B, WASM) with numeric state living IN the event log | `ledger.py` (all of it); refusal log §5 |
| G-2: Seed King Halric at tier 4 (TASK.md's own scale) | `ActorCreatedPayload` validates `tier` ge=0 le=3 — tier 4 raises ValidationError | Seeded at tier 3 (same protection ceiling as any T2+) | cosmetic | Nothing (docs mismatch), or a wider authored-tier scale | `uro_core/domain/events.py:153` vs `realm.py` ACTORS |
| G-3: `create_thread` under faction scope (a faction's rule spawns a plot about that faction) | Gauntlet checks the NEW thread id against the scope's allowed refs; a faction scope allows only the faction + members → silently dropped. Works only with `scope {thread: <the-new-id>}` — a rule scoped to a thing that doesn't exist yet | r4a self-scopes to the thread it creates (works, reads oddly) | annoyance | Allow create_thread under faction/place scope, or document self-scoping as the pattern | `uro_core/engines/rules_gauntlet.py:73`; `realm.py` r4a |
| G-4: Reflecting sim results via `append_beat` should trigger the Reaction Layer | `append_beat` commits + projects but never runs rules; only `Engine._finish` and the server outcome route call `react()`. Our R1/R4 were silently dead for authored/Chronicler events until the game called `engine.react` itself | Every game append goes through `commit_authored()` which calls `engine.react(campaign, commit_id, events)` manually (mirrors uro-server) | major | An "append with reactions" entry point (or react folded into append_beat behind a flag) so embedders can't forget the second call | `sable_court.py commit_authored`; `uro_core/pipeline/engine.py:315` vs `store.py:769` |
| G-5: An unaliased colloquial handle ("the Salt Knight" — actually Ser Garrick) should resolve or be mergeable | Canonical-name + alias matching found no match → gauntlet minted a brand-new T1 actor; no merge primitive, no alias-add event to repair post-hoc | Author aliases up front for every colloquial handle (done for 15 nobles); the Salt Knight stays fragmented as the exhibit | annoyance | An `AliasAdded` event (cheap), and/or the deferred embedding `entity_index` (OQ-3) + an actor-merge event | `script.py` beat 8; `uro_core/pipeline/extraction.py:142` |
| G-6: Tie a battle's outcome to WHEN it happened in world time | `OutcomeBundle` carries no time; its events commit at whatever world_time the branch holds; `duration_rounds` is decorative (known limit #2, confirmed hit) | Careful call ordering (tick first, battles after) so the day is right by construction | annoyance | An optional world_time / day offset on the bundle or `distill_outcome` | `uro_core/chronicler.py` OutcomeBundle; `sable_court.py run_tick` order |
| G-7: A great lord (tier ≥ 2) can die in a battle the realm sim resolved — this game is ABOUT killing great lords | `distill_outcome` downgraded the Marshal's casualty to a truth=unknown "is said to have fallen" claim and dropped the loot of his letters; only the T0 levy died. Same for the King's assassination — so r6 (succession-on-death) is authored yet **unfireable**: no runtime path can emit ActorDied for protected canon | Great-lord deaths become unconfirmable rumors (good court flavor!) — but true succession-by-assassination is **BLOCKED** | major | A trusted-consumer tier for embedders (Posture A holds root via append_beat anyway) — e.g. `distill_outcome(trust="embedder")`, or a sanctioned lethal authored-event path with its own gauntlet | `uro_core/chronicler.py:148`; `sable_court.py` salt-road + knife-in-the-dark bundles |
| G-8: Thread lifecycle management at dozens of live plots | Every active/offered thread is injected into every narrator prompt (`active_threads` is campaign-wide, unscoped); nothing ever retires/expires/ranks a plot — 18 live plots in every prompt by the end | Game resolves threads by hand-appending `ThreadStateChanged`; no relevance ranking possible from outside | major | Thread relevance scoping in recall (entity-linked or embedding-ranked) + a lifecycle policy (age-out, cap, or rule-drivable resolve) | `uro_core/pipeline/recall.py:96`; stage-4 probe |
| G-9: The narrator should know p:border-march changed hands (it IS in proj_places + the owns graph) | `assemble_recall` has NO place channel (RecallBundle: beats/actors/claims/beliefs/memories/threads), and claim relevance matches only actor ids and name-tokens — a claim with `subject_refs=["p:border-march"]` is unreachable even when the intent names the place | Record every place fact TWICE: keyed to the place id (state) and to a name-token (recall) — a duplication smell | major | A place-state recall channel (known limit #8) + entity-ref matching for p:/f: refs in claim relevance | `uro_core/pipeline/recall.py:73`; `sable_court.py transfer_holding` |
| G-10: `fork_branch` should fork the WHOLE game state | The fork copies every Uro projection at the commit — but the shadow ledger lives in game code, so the game must snapshot/restore it manually at the exact fork commit; miss it and both lines silently share numeric fate | `copy.deepcopy(self.ledger)` at the instant fork_commit is captured; fragile — every potential fork point needs one | major | The strongest argument FOR engine-owned computation: state the engine owns forks for free; every number forced into game code breaks the signature feature | `sable_court.py` stage 3 deepcopy + stage 5 restore |
| G-11: Keep playing the same campaign on a fork | A Campaign is pinned to its branch_id; the fork carries the PC binding, but run_beat on the old campaign would commit to the OLD branch | `start_campaign(fork_branch, adopt_actor_id="a:spymaster")` — works but undocumented as THE fork-play pattern, and emits a second CampaignStarted | annoyance | A documented/first-class `fork_campaign(campaign_id, at_commit)` | `sable_court.py` stage 5 |
| G-12: A cross-House rule (war edge between two factions) under an honest scope | The gauntlet requires BOTH edge endpoints inside ONE faction's jurisdiction; scoped to f:vaelric the action is dropped with no error and no author-visible log (r5b proves it: its rumor simply never exists) | An umbrella faction `f:court` that every House is `member_of`, purely to grant realm-wide rules jurisdiction (r5a) — a modeling hack | major | A `world` scope (explicit whole-realm jurisdiction) + drop diagnostics (a projected module-audit trail of dropped actions) | `uro_core/engines/rules_gauntlet.py:88`; `realm.py` r5a vs r5b |

## 3. Top 3 things Uro MUST add for this game to be good

1. **The engine-owned computation tier (D-33 Stage B)** — G-1 + G-10 + refusal log §5. Not just
   ergonomics: because the numbers live outside Uro, the engine's *signature feature* (fork) no
   longer covers the whole game state, and rules can never see the realm's most important facts
   (strength, gold, tension). Twelve concrete wished-for rules below are the evidence gate.
2. **Recall channels for non-actor state** — G-9 + G-8. Place ownership changes hands and the
   narrator is provably blind (the stage-4 probe dumps `RecallBundle` — no place channel, and
   p:-keyed claims are unreachable); meanwhile all 18 live plots flood every prompt unranked.
   One recall increment (place channel + entity-ref claim matching + thread scoping) fixes both.
3. **Reaction-Layer integration for embedders** — G-4 + G-12 + G-3. `append_beat` silently
   bypasses rules (we lost R1 to it), out-of-scope actions drop with zero diagnostics (r5b), and
   cross-faction jurisdiction needs an umbrella-faction hack. An append-with-react entry point,
   a `world` scope, and a dropped-action audit trail would make the reaction layer trustworthy
   for a consumer that authors events.

## 4. Verdict on targeted leftover-work

- **Reaction-Layer expressiveness ceiling:** **Hit — yes, squarely.** The deferral (declarative
  grammar first, WASM tier reserved) was the right call to *ship* D-33, but it is **now blocking
  a real consumer**: none of §2.3's realm sim fits the grammar — 12 wished-for rules refused
  (§5), the whole economy/tension/war engine lives in `ledger.py`, and the shadow state breaks
  forking from the outside (G-10). Evidence: `ledger.py` + `sable_court.py run_tick` +
  refusal log §5. The computation-shaped use-case D-33 said to wait for has arrived.
- **Single-dimension scope wrinkle:** **Hit — yes.** The wished single rule (create the
  counter-pact thread AND stir the guild with one alliance trigger) had to ship as two rules,
  r4a (thread-scoped, self-scoping to a thread that doesn't exist yet — G-3) and r4b
  (faction-scoped); RL-9 shows the single rule we wanted. At 6 rules the split is an
  annoyance; at realm scale (every reaction touching a plot + a House + a place) it triples
  rule count and lets halves drift apart. **Deferral tolerable, ergonomics cost real** —
  add a `world` scope and multi-scope before packs grow. Evidence: `realm.py` r4a/r4b, RL-9.
- **OQ-8 blast radius:** **Hit — yes.** Single-hop is real: `react()` explicitly never
  re-triggers on module events (engine.py:339), so r4a's counterplot can never breed whispers
  (RL-12); the war cascade (allies dragged in, holdings changing hands, landless Houses) was
  computed entirely in game code and reflected hop by hop (RL-11); `agenda_tick` applies
  headers + declarative agendas but no ripple. **For a realm sim, single-hop-only is a
  blocker-shaped major** — the realm's most interesting behavior (cascades) is exactly what
  the engine refuses to run. Evidence: `uro_core/pipeline/engine.py:339`,
  `sable_court.py sync_wars_from_uro`/`reflect_tick`, RL-11/RL-12.
- **Place-state recall gap:** **Hit — yes, with a dump.** `p:border-march` transferred to
  Corvane (owns edge + description updated — `get_place` shows it), then the probe intent
  "whose banners fly there now?" assembled recall: `RecallBundle` has **no place field at
  all**, and the id-keyed transfer claim was invisible while the identical name-token-keyed
  claim surfaced (printed side by side in stage 4). **The deferral is now blocking**: a game
  about holdings cannot let the narrator see holdings change. Evidence: stage-4 probe output;
  `uro_core/pipeline/recall.py:73`; G-9.
- **Entity resolution at scale:** **Hit — yes, and canonical-name + alias held.** Garret/
  Garrick/Gareth, Aldric vs Aldrice vs Aldric-the-Younger, "the Marshal"/"Lady Corvane"/"the
  Younger" all resolved correctly through authored aliases across 11 beats (0 false merges,
  asserted); the one unaliased handle ("the Salt Knight") fragmented into a new actor exactly
  as predicted — and there is no merge/alias-add path to repair it (G-5). **Verdict: the
  Layer-2 deferral (no entity_index) remains right for a court of dozens IF aliases are
  authored diligently**; the missing piece is cheap post-hoc repair (AliasAdded), not
  embeddings. Evidence: stage-1/2/4 asserts; `extraction.py:142`.
- **Thread lifecycle at scale:** **Hit — yes.** 12 seeded → 27 threads by run's end (counts
  printed each phase, growth monotonic); 18 active plots go into EVERY narrator prompt;
  nothing engine-side ever closes one — the only path is the consumer appending
  `ThreadStateChanged` (we resolved t:tax-revolt by hand to prove it exists). **Deferral
  now biting**: fine at 12 threads, visibly flooding at 18 active, untenable at CK-scale
  hundreds. Needs recall scoping/ranking more urgently than auto-close. Evidence: stage-4
  output; `recall.py:96`; G-8.

## 5. THE REFUSAL LOG (headline — the WASM-tier evidence gate)

Every realm rule the declarative grammar could NOT express. Written as the exact `rule_pack`
entry we wished we could write; all 12 are also printed live by the game (stage 6), each logged
at the call site that needed it.

### RL-1 — Tension counter + threshold + reset
Wished-for rule:
```jsonc
{ "id": "tension-boils-to-war",
  "trigger": {"event": "ClaimRecorded", "where": {"origin": "hostile-intrigue"}},
  "then": [{"do": "increment_counter", "counter": "tension(f:vaelric,f:corvane)", "by": 1},
           {"do": "add_edge", "src": "f:vaelric", "rel": "at_war_with", "dst": "f:corvane",
            "if": "tension(f:vaelric,f:corvane) >= 5"}],
  "scope": {"faction": "f:court"} }
// and: a brokered marriage RESETS the pair's counter to 0
```
Missing primitive: a per-pair accumulating counter + threshold trigger + reset — the grammar has
no variables or state at all (conditions read projections; actions write a fixed union).
Where the game needed it: `ledger.py` `ShadowLedger.tension` / `add_tension` / `tick` step 3.

### RL-2 — Economy: income minus upkeep, distress sale on deficit
Wished-for rule:
```jsonc
{ "id": "house-economy",
  "every_days": 20,
  "then": [{"do": "for_each", "faction": "*", "as": "H", "do": [
             {"do": "set", "var": "gold(H)",
              "expr": "gold(H) + sum(holding_value(p) for p owned_by H) - upkeep * strength(H)"},
             {"do": "transfer_holding", "if": "gold(H) < 0",
              "from": "H", "to": "f:argent", "pick": "lowest_value"}]}],
  "scope": {"faction": "f:court"} }
```
Missing primitive: arithmetic (sum/multiply), accumulating gold state, a conditional loop over
holdings and over factions.
Where the game needed it: `ledger.py` `ShadowLedger.tick` steps 1–2 (income + distress sale).

### RL-3 — Comparative war trigger (cross-entity numeric compare)
Wished-for rule:
```jsonc
{ "id": "predator-smells-weakness",
  "every_days": 30,
  "when": {"kind": "compare", "left": "strength(f:vaelric)",
           "op": ">", "right": "strength(f:corvane) * 1.2"},
  "then": [{"do": "add_edge", "src": "f:vaelric", "rel": "at_war_with", "dst": "f:corvane"}],
  "scope": {"faction": "f:court"} }
```
Missing primitive: cross-entity numeric comparison — `when` can only compare a fixed projection
field (tier, world_day) to a constant; strength does not even exist engine-side.
Where the game needed it: `ledger.py` `ShadowLedger.tick` step 4 (`roll = strength + d6*scale`).

### RL-4 — Weighted outcome table for a battle's aftermath
Wished-for rule:
```jsonc
{ "id": "fortunes-of-war",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "at_war_with"}},
  "then": [{"do": "roll_table", "weights": {"defection": 40, "siege": 30, "truce": 30},
            "outcomes": {
              "defection": [{"do": "remove_edge", "src": "a:captain-hurn",
                             "rel": "member_of", "dst": "f:vaelric"}],
              "siege":     [{"do": "set_thread_state", "thread": "t:border-war", "to": "active"}],
              "truce":     [{"do": "remove_edge", "src": "f:vaelric",
                             "rel": "at_war_with", "dst": "f:corvane"}]}}],
  "scope": {"faction": "f:court"} }
```
Missing primitive: weighted RNG / outcome tables — the grammar is fully deterministic and the
engine offers no seeded-RNG surface to rules.
Where the game needed it: `ledger.py` `ShadowLedger.tick` step 4 (`rng.randint` war dice).

### RL-5 — Fall of a House (count-to-zero + iterate members)
Wished-for rule:
```jsonc
{ "id": "fall-of-dellmoor",
  "trigger": {"event": "EdgeRemoved", "where": {"rel_type": "owns"}},
  "when": {"kind": "count", "what": "edges(src=f:dellmoor, rel=owns)", "op": "==", "value": 0},
  "then": [{"do": "set_thread_state", "thread": "t:dellmoor-decline", "to": "resolved"},
           {"do": "for_each", "member_of": "f:dellmoor", "as": "M",
            "do": [{"do": "remove_edge", "src": "M", "rel": "member_of", "dst": "f:dellmoor"}]}],
  "scope": {"faction": "f:dellmoor"} }
```
Missing primitive: counting a projection set to zero + iteration over a faction's members.
Where the game needed it: `ledger.py` `ShadowLedger.tick` (`rep.landless` / `rep.broken`).

### RL-6 — Recruitment (integer division, spend-to-buy loop)
Wished-for rule:
```jsonc
{ "id": "vaelric-raises-levies",
  "every_days": 20,
  "when": {"kind": "compare", "left": "gold(f:vaelric)", "op": ">=", "right": 10},
  "then": [{"do": "set", "var": "strength(f:vaelric)",
            "expr": "strength(f:vaelric) + gold(f:vaelric) // 10"},
           {"do": "set", "var": "gold(f:vaelric)", "expr": "gold(f:vaelric) % 10"}],
  "scope": {"faction": "f:vaelric"} }
```
Missing primitive: integer arithmetic + mutable numeric state (strength/gold are not engine
concepts).
Where the game needed it: `ledger.py` `ShadowLedger.tick` step 2 (ambition "expand").

### RL-7 — Influence accumulation toward a coup threshold
Wished-for rule:
```jsonc
{ "id": "the-ledger-buys-the-throne",
  "every_days": 30,
  "then": [{"do": "increment_counter", "counter": "influence(f:argent)", "by": 1},
           {"do": "set_thread_state", "thread": "t:argent-debt", "to": "active",
            "if": "influence(f:argent) >= 12"}],
  "scope": {"faction": "f:argent"} }
```
Missing primitive: an accumulating per-faction counter readable in a later condition.
Where the game needed it: `ledger.py` `House.influence` / `tick` step 2 (ambition "ascend").

### RL-8 — Rumor decay / expiry
Wished-for rule:
```jsonc
{ "id": "gossip-goes-stale",
  "every_days": 30,
  "then": [{"do": "expire_claims", "where": {"origin": "module"},
            "older_than_days": 60}],
  "scope": {"faction": "f:court"} }
```
Missing primitive: temporal state on claims (age) + any action that retracts/decays a claim —
the action union can only ADD claims; module rumors accumulate forever (each r2 cadence even
re-mints the identical rumor text as a fresh claim).
Where the game needed it: `sable_court.py` stage 4 (rumor sets only ever grow across ticks).

### RL-9 — One rule touching a thread AND a faction (the scope split)
Wished-for rule:
```jsonc
{ "id": "alliance-echoes",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "allied_with"}},
  "then": [{"do": "create_thread", "thread": "t:counter-pact",
            "stakes": "A counter-pact forms against the new alliance."},
           {"do": "record_rumor", "text": "The Ledger reprices every debt by dawn.",
            "subjects": ["a:maren-argent"]}],
  "scope": {"thread": "t:counter-pact", "faction": "f:argent"} }  // TWO scopes: forbidden
```
Missing primitive: multi-dimension scope — `Scope` allows exactly one of thread|faction|place,
so this one reaction ships as two rules (r4a thread-scoped + r4b faction-scoped) that can drift
apart.
Where the game needed it: `realm.py` RULE_PACK r4a/r4b (the split we actually shipped).

### RL-10 — Succession that can actually open (a death the trust model permits)
Wished-for rule:
```jsonc
{ "id": "r6-succession-opens-on-kings-death",  // SHIPPED — dead code:
  "trigger": {"event": "ActorDied", "where": {"actor_id": "a:halric"}},
  "when": {"kind": "thread_state", "thread": "t:succession", "state": "dormant"},
  "then": [{"do": "set_thread_state", "thread": "t:succession", "to": "active"}],
  "scope": {"thread": "t:succession"} }
```
Missing primitive: any runtime path that can emit ActorDied for a T2+ actor: combat is
non-lethal, the Chronicler ceiling downgrades, rules can't kill — a rule triggered on a
protected actor's death can never fire (the grammar's trigger vocabulary outruns what the trust
model lets happen).
Where the game needed it: `realm.py` r6 + `sable_court.py` stage-3 assassination asserts.

### RL-11 — Transitive alliance cascade (allies dragged into a declared war)
Wished-for rule:
```jsonc
{ "id": "the-web-of-oaths",
  "trigger": {"event": "EdgeAdded", "where": {"rel_type": "at_war_with"}},
  "then": [{"do": "for_each", "traverse": "allied_with", "from": "$trigger.src", "as": "ALLY",
            "do": [{"do": "add_edge", "src": "ALLY", "rel": "at_war_with",
                    "dst": "$trigger.dst"},
                   {"do": "increment_counter", "counter": "tension(ALLY, $trigger.dst)",
                    "by": 2}]}],
  "scope": {"faction": "f:court"} }
```
Missing primitive: graph traversal + trigger-payload binding (`$trigger.src`) + iteration —
rules see only fixed, literal entity ids; every multi-hop consequence (ally-of-ally goes wary,
dependent plots wake) was computed in game code (OQ-8's blast radius, hand-rolled).
Where the game needed it: `sable_court.py sync_wars_from_uro` / `ledger.py` (the cascade the
game hand-rolls).

### RL-12 — Reactions that cascade (a rule triggered by a module event)
Wished-for rule:
```jsonc
{ "id": "counterplot-breeds-whispers",
  "trigger": {"event": "ThreadCreated", "where": {"provenance": "module"}},
  "then": [{"do": "record_rumor", "text": "Something moves against the new pact.",
            "subjects": ["a:maren-argent"]}],
  "scope": {"faction": "f:argent"} }
```
Missing primitive: module events never re-enter `react()` (single-hop by design, no cascade
budget) — r4a's counterplot can never itself breed consequences.
Where the game needed it: `uro_core/pipeline/engine.py:339` ("module events do not re-trigger
it").
