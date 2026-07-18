# IRONWAKE — Uro Gap Report

## 1. Summary

Uro's current surface carried this game: a full 8-contract season runs deterministically on the
stub provider in both postures (asserted by `tests/test_server_posture.py`, which replays the
season over HTTP/WS and matches the embed run's digest), permadeath is real committed canon,
feats become witness rumors that hedge with distance, the protection ceiling turned the Headhunt
into the game's best story beat, and one `fork_branch` call produced a genuinely divergent
ending from the same chronicle — with the fork's roster reconstructable from pure canon reads
because deaths live in Uro. The single biggest wall was the **missing read surface around the
Chronicler write path**: a game that reports over HTTP cannot read ANYTHING back over HTTP — not
its roster, not its rumors, not even which parts of the bundle it just POSTed were accepted
versus downgraded — so every posture collapses back into the embedded Python library, and the
"network game" posture is a fiction for any consumer not co-located with Postgres. Close behind
it: the declarative Reaction Layer could express none of the seven counter/quantifier rules the
game wanted — six refused outright, and the seventh **validated yet can never fire** (a
where-filter on a payload key that doesn't exist passes the grammar silently) — and each counter
that fell into game code then silently failed to fork, the same finding sable-court hit,
reproduced here from the Chronicler side.

## 2. Gap Table

| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity (blocker\|major\|annoyance\|cosmetic) | What Uro would need (concrete engine change) | Evidence (the call/file that hit it) |
|---|---|---|---|---|---|
| Read roster/chronicle/rumors over HTTP to render towns in `--posture server` | The entire HTTP surface is WS `/play` + `POST /outcome` + `/healthz`; every read (`list_actors`, `claims_about`, `beliefs_of`, `list_threads`, `items_owned_by`, `current_world_time`) had to go through a second in-process library connection to the same Postgres | Library reads even in server posture (`world/reads.py` runs embedded) | blocker | REST read surface: `GET .../actors`, `/actors/{id}`, `/claims?about=`, `/beliefs/{actor}`, `/threads`, `/edges?rel=`, `/items?owner=`, `/time`, `/campaigns/{c}/chronicle` | `world/uro.py log_server_read_gap`; all of `world/reads.py` (G-2 in the run log) |
| Learn from the outcome POST what was accepted vs downgraded/refused | `POST /outcome` returns only `{committed_events, commit_id}` — the Vorlund downgrade is invisible in the response; even `distill_outcome` (embed) returns a bare event list with no per-ref verdicts | `world/chronicle.py read_back` re-reads canon and diffs it against the bundle | major | A structured ingestion receipt: per-ref `{committed \| downgraded \| dropped, reason}` from both `distill_outcome` and the endpoint | `world/uro.py ServerHandle.post_outcome` (response schema); `chronicle.py read_back` |
| Resolve a kill-the-named-boss contract through the Chronicler | Vorlund is tier 2: his casualty downgraded to a `truth=unknown` "Captain Vorlund is said to have fallen at e:c4-headhunt." claim, the blade loot was refused, he stayed `status=alive` — twice (c4 and c8) | Dramatized: the warlord refuses rumor as proof, the bounty thread stays `active`, Vorlund returns for the finale | major | An authorized channel for protected canon (e.g. parked-encounter registry entry pre-authorizing named participants for one declared encounter) | `cli/season.py dramatize_headhunt`; `uro_core/chronicler.py:148-164`; season run: `DOWNGRADED to rumor (protected): Captain Vorlund`, `loot REFUSED: i:vorlunds-blade` |
| The Reaction Layer owning ANY of the game's counters | The closed grammar (worldpack/rules.py Condition/Action unions) has no counter, accumulator, arithmetic, aggregate, join, or resource primitive; a live `RulePack(**counter_rule)` attempt fails pydantic validation (`rules.0.when` discriminator rejection, printed every run) | 6 of 7 wished rules refused outright and rewritten as shadow counters in `game/company.py` (wins, total_kills, red_band_dead, bounty_failures, roster count, gold); the 7th worked around by an over-broad any-death trigger (next row) | major | The reserved scripting tier (D-33 Stage B) or at minimum engine-owned counters (`{kind: "counter"}` condition + `increment` action) | `cli/season.py counter_wall` (live refusal); `world/rules.py WISHED_RULES` — the 7-entry refusal log printed every run |
| A trigger quantified over a faction's members ("on ANY Red Band death") — and a parse-time check that a `where` filter CAN ever match | **RESOLVED (RL-6).** Two fixes landed: (1) the dogfood-findings work made `RulePack` reject a `where` key that is not a real payload field, so `{"actor.member_of": "f:red-band"}` is now REFUSED loudly, not accepted-but-inert; (2) RL-6 shipped the real primitive — a plain `ActorDied` trigger + a `$trigger.<field>`-aware `when` (`edge_exists src=$trigger.actor_id rel=member_of dst=f:red-band`, `RULES_API_VERSION` 5) evaluated per matching death, so the member-of existential is directly expressible, with `trigger.per_event` for the count-each shape | none — the war ratchet can now be member-scoped instead of any-death | minor (was major) | Only multi-hop/transitive triggers (a member of an ALLY of X) still need `for_each` traversal in conditions — reserved | `cli/season.py counter_wall` (live: footgun REFUSED + member-of trigger EXPRESSIBLE); `uro_core/engines/rules.py`, `worldpack/rules.py` (RL-6) |
| Fork the WHOLE game state when `fork_branch` forks the world | `fork_branch` forked every projection perfectly — the fork's roster is a pure canon read (mercs dead only in the finale are alive on the fork) — but gold/wins/kill-counters exist only in game code *because the grammar refused them*, and that shadow state does not fork | Roster re-read from `get_actor(fork).status`; purse/counters knowingly wrong on the fork | major | Same as above: engine-owned numeric state rides forks for free; every refused counter is also a fork-consistency bug | `cli/season.py what_if_fork` (roster re-read); confirms sable-court's headline finding from the Chronicler side |
| Participant scope that fully fences a self-attested bundle | Out-of-cast feats/loot/witnesses are dropped, but an out-of-cast CASUALTY of any existing actor still mints a public `truth=unknown` "said to have fallen" claim; and the scope root itself is whatever `participants` list the game asserts | None needed for an honest game; probed once on a throwaway fork (`scope-probe`) | major | Parked-encounter registry (pre-declared cast, bundle validated against it); treat out-of-cast casualties like out-of-cast feats (drop, or scope-check) | `cli/season.py adversarial_probe`: Mira alive ✓, feat dropped ✓, loot refused ✓, BUT "Mira is said to have fallen at e:probe-scope." exists; `chronicler.py:148` else-branch |
| Map the game clock (contract days) onto `world_time` | No mapping exists; the game invented "travel×2 + 1 battle + 2 rest" and hand-ticks `agenda_tick` per contract. Wart: `evaluate_agendas` fires a rule at most ONCE per tick regardless of how many cadence boundaries the skip crossed, so agenda cadence silently depends on the caller's ticking style | One `agenda_tick` per contract cycle; convention documented in `cli/season.py` | major | Register a game-clock→world-day mapping at campaign start; fire agendas once per crossed boundary (bounded) | `cli/season.py downtime_phase`; `uro_core/engines/rules.py:145` (`to_day//every > from_day//every`) |
| Tune how far the company's legend travels (3+ towns) | `distill_outcome` hardcodes `propagate_belief` defaults (0.9 base, 0.55 decay, 0.2 floor): a rumor dies exactly 2 hops past a witness — Greywater (3 hops) hears NOTHING, every time | Authored the `knows` chain so towns that matter sit within 2 hops; Greywater's silence dramatized | major | Per-bundle (or per-world) propagation parameters on the Chronicler surface | `cli/season.py rumor_ripple` (run log: "Greywater (3 hops): has heard NOTHING"); `chronicler.py:139` |
| Hop-to-hop distortion of the rumor's WORDS | Confidence decays over the SAME claim text: Corin at 0.272 is *unsure of the exact words* of Gerhardt's deed, never misremembering different ones — no garbling, embellishment, or misattribution | BLOCKED (phrasing does hedge via the confidence→certainty rendering, which is real and works) | major | A garbled-statement model: per-hop claim variants (template or LLM) chained via `learned_from` | `engines/actor.py` docstring ("a later refinement"); run log: identical statement at 0.495 and 0.272 |
| A commit-boundary check on WHO may append WHAT | The game hammers `append_beat` all season (enemy musters, contract threads, recruit edges) and nothing ever checks whether an embedding caller should be allowed to — the same call would accept `actor_died(a:vorlund)`, bypassing the ceiling `distill_outcome` enforces | Discipline: IRONWAKE appends only its own muster/lifecycle event types (trusted by policy) | major | Append-time emitter whitelist (caused_by-keyed event-type allowlist for embedding consumers) | `world/chronicle.py muster_events` + `cli/season.py` append call sites |
| One Chronicler ingestion call for embedded games | `distill_outcome` neither commits nor reacts; the embed caller must know to `append_beat` AND `engine.react` — the server endpoint does both, and forgetting `react()` silently kills every `ActorDied`-triggered pack rule (the war ratchet) | `world/chronicle.py report_embed` wraps the three calls | annoyance | A library-level `report_outcome(branch, bundle)` mirroring `uro_server.app`'s path | `world/chronicle.py report_embed` vs `uro_server/app.py engine_deps.report_outcome` |
| Loot lands on someone who can carry it off the field | `distill_outcome` validates the item exists, `from_ref` owns it, both refs in cast — but never that `to_ref` survived: an early seed-7 run committed the Red Band standard to a merc who DIED in the same battle, and the purse paid out on a lost field | Game clamps `to_ref` to a living merc and only loots a won field | annoyance | `to_ref` liveness (not-in-`casualties`) validation in the loot gate | `world/chronicle.py derive_loot`; `uro_core/chronicler.py:166-182` |
| Author an `at_war_with` edge between two factions from an agenda | The gauntlet drops `add_edge` unless BOTH endpoints are inside the rule's single scope, and a faction scope contains only the faction + its `member_of` members — two belligerents share no natural scope | Seeded a fictional meta-faction `f:the-marches` with both belligerents as members, purely to satisfy the fence | annoyance | Multi-ref scopes (`{factions: [a, b]}`) or edge actions scoped by either endpoint | `world/rules.py agenda-war-drums` + `world/setup.py THEATER`; `rules_gauntlet.py _scope_refs` |

## 3. Top 3 Things Uro MUST Add (for this game to be good)

1. **A REST read surface + structured ingestion receipts** (rows 1–2) — why #1: it invalidates
   the entire network posture today. IRONWAKE's server mode is a pretense: writes go over HTTP,
   then the game opens a second, in-process Python connection to render a tavern or find out
   whether its own bundle was accepted. Any Chronicler game not written in Python on the same
   host as Postgres is currently **impossible**, and even a Python one cannot see downgrades
   without diffing projections by hand.
2. **Engine-owned counters / the scripting tier** (rows 4–6) — why #2: the grammar's missing
   counter is not one inconvenience, it is seven wished rules (six loud refusals plus one
   accepted-but-inert quantifier; the full refusal log prints with every run), and the cost
   compounds: every counter pushed into game code is ALSO a fork-consistency bug, because
   `fork_branch` forks everything Uro owns and nothing it refused to own. The two flagship
   features (Reaction Layer, branching) undermine each other through this hole.
3. **An authorized channel for protected canon + a parked-encounter registry** (rows 3, 7) —
   why #3: the ceiling as-shipped is a genuinely great *rumor* mechanic (the Headhunt beat is
   the best scene in the game), but a whole genre — "kill the named boss" — is structurally
   unwritable, and the same trust boundary leaks the other way (out-of-cast casualties mint
   gossip about anyone). One registry closes both: pre-declared casts make scope checkable and
   give a trusted game a place to be granted protected-canon authority for one encounter.

## 4. Verdict on Targeted Leftover-Work

- **Full Chronicler contract / self-attested scope (OQ-12):** HIT — 10 legitimate bundles per
  season across both postures (the server leg asserted by `tests/test_server_posture.py`), plus
  one adversarial probe on a throwaway fork. The enforcement
  that exists (drop out-of-cast feats/loot/witnesses, existence+ownership, idempotent replay —
  the retry probe committed nothing twice) held exactly as documented. But the scope root is
  self-asserted: Uro could NOT trust IRONWAKE — we seated Corin as a "witness" of the ferry
  fight by fiat, and Uro had no way to know if that was fiction. A parked-encounter registry
  would have cost us one extra call per contract (declare the cast at muster time — we already
  have that exact moment in `muster_events`) and would close both the fake-witness and the
  out-of-cast-casualty-gossip holes. **Deferral was reasonable for a first consumer; now that a
  real consumer exists, build it** — the muster/report call pattern it needs is demonstrated
  here.
- **Game↔world time mapping:** HIT — the invented convention (travel×2+1+2, one `agenda_tick`
  per contract) worked but is arbitrary, and the once-per-tick agenda semantics make world
  cadence depend on the caller's ticking style (an 11-day tick over a 10-day agenda ≠ eleven
  1-day ticks: day 9→20 crosses two cadence boundaries but fires once). **Deferral
  half-right:** a full mapping is genuinely game-specific, but per-boundary agenda firing is an
  engine bug-shaped wart that should be fixed regardless of any mapping.
- **Protection ceiling (learning contract):** HIT, twice (c4 ford, c8 palisade) — downgrade to
  `truth=unknown`, blade refused, bounty unpaid, all asserted. Verdict: **feature AND blocker.**
  As a rumor engine it is superb — "the man you killed stands on the palisade" is the game's
  best beat and emerged from the trust model, not despite it. But a kill-the-boss contract is
  unresolvable BY DESIGN with no escape hatch, which walls off a genre. Smallest safe change:
  scope-limited authority via the parked-encounter registry (a registered encounter whose
  pre-declared cast includes the protected actor MAY commit that actor's death), keeping the
  blanket ceiling for unregistered bundles.
- **Rumor propagation + confidence decay + statement-distortion gap:** HIT — the decay chain is
  real and legible (0.9 witness / 0.495 home "believes" / 0.272 far "has heard a rumor"), it
  reaches the narrator prompt as hedged phrasing, and it made the tavern scenes work. Two real
  limits: the hardcoded floor gives a hard 2-hop horizon (a 3rd town hears NOTHING, and the
  game cannot tune it), and the words never garble — for a rumor-centric game the missing
  statement distortion costs the whole "legend grows in the telling" fantasy; what we'd want is
  a per-hop derived claim chained via `learned_from`. **Deferral of distortion was right for a
  PoC; the untunable propagation parameters should be opened now (trivial API change).**
- **Witness semantics (zero-survivor silence):** HIT — the Silent Mill wiped both sides at seed
  7 (and the collapse makes it live at any seed); deaths committed, and NO belief reached any
  town NPC — asserted per-NPC. It behaved correctly and it FELT right (the run prints "The
  Marches will never know how they fought"). The surprise was on the other side: routed enemies
  are witnesses, so a fled raider carried "Elke held the ferry gate" to Vorlund himself via a
  `knows` edge — emergent, cheap, and excellent. One asymmetry worth knowing: a PROTECTED actor
  who flees (tier-2 Vorlund routed in an early tuning run) is barred from being a witness of
  what he personally saw. **Deferral of nothing needed here — this subsystem is done and good.**
- **Missing REST management surface:** HIT, hardest — see Top-3 #1. `--posture server` writes
  over HTTP/WS but performs its ~15 distinct read call-sites through the embedded library; the
  needed endpoints are enumerated in the gap table row. **Now BLOCKING a real consumer class**
  (any non-Python, non-co-located game).
- **Append-time emitter whitelist:** HIT by construction — the game's own legitimate write path
  (enemy musters, contract threads, recruit edges via `append_beat`) is exactly the path that
  could commit `actor_died(a:vorlund)` with one line. Nothing felt unsafe *for us* because we
  are the same process as the engine; but the D-32 ceiling is only as strong as its narrowest
  gate, and embedding games sit inside it. **The deferral ("by-policy invariant") was right for
  a PoC, but IRONWAKE demonstrates the ceiling is advisory for Posture-A consumers — worth an
  allowlist before any third-party embedding story.**
- **Reaction Layer scripting tier (counters):** HIT — two live probes per run: pydantic rejects
  `{kind: "counter"}` at `rules.0.when` (the loud refusal), and the quantified member-of
  trigger is ACCEPTED yet provably inert (the quiet one). The 7-entry refusal log covers the
  rules IRONWAKE wanted in the exact pack syntax (win-count war trigger, kill-count reputation,
  per-5-deaths escalation, failure-priced bounty, roster-count desperation, member-of
  quantified trigger — the accepted-but-inert one, canon-conditioned pay). What the grammar
  COULD express was genuinely useful (the death-ratchet war state machine, the smoulder agenda,
  and the standard-falls resolution rule all did real work), which sharpens the verdict:
  **IRONWAKE is a second independent consumer proving the counter/aggregate tier is needed** —
  not for exotic scripting, but for the most ordinary game logic there is (wins, kills,
  prices), and doubly so because refused counters break fork consistency (gap row 6).
