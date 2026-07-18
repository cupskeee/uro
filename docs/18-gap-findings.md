# 18 — Gap findings (the games as forcing functions)

*The evidence-backed engine backlog, derived from four games built ON Uro (2026-07-12).*

Four games were built as **forcing functions** (`examples/games/`), each aimed at a different
cluster of Uro's deferred / by-policy / unproven work. Each shipped a `GAP_REPORT.md`; this doc
synthesizes them into a prioritized backlog. The point of the exercise: stop *guessing* which
leftover work matters and let real consumers show us, with evidence, where the engine bends. A
finding's weight here is its **cross-game corroboration** — a wall three independent games hit is a
priority; one game's edge case is not.

- **Ironwake** (Chronicler tactics) · **The Sable Court** (realm sim) · **The Seventh Vault**
  (co-op heist, WS) · **Hollowloop** (time-loop roguelike). Full reports in each game folder.

## Landed already (2026-07-12, commit `3d82151` + migration 014)

Confirmed against the code, in-scope, small — fixed immediately:

1. **Fork hot-path `Seq Scan`** (Hollowloop G-10). `_copy_memory` filtered `memory_index` on
   `commit_id`, indexed only on `branch_id` → every fork scanned a *global* table (30–60% of fork
   time, unbounded as the DB grows). **Fixed:** migration 014 adds `memory_index_commit_idx`.
2. **`PartyArbiter` misrotation on holder-disconnect** (Seventh Vault G-17). `note_left` stepped the
   cursor backward when the turn-holder left from a non-zero cursor (double turn / skipped player);
   the existing test masked it. **Fixed:** strict `idx < cur` + wrap, +2 regression tests.
3. **Accepted-but-inert triggers** (Ironwake RL-6, Seventh Vault S7, Hollowloop — *3 games*). A rule
   with an unknown `trigger.event` or a `where` key that isn't a real payload field validated
   silently and never fired. **Fixed:** a `Rule` validator checks event type + where-keys against
   the domain payload models at parse and at `create_world`.
4. **Silent rule-pack death** (Hollowloop G-6). A bad pack was swallowed by the runtime
   exception-isolation → one typo darkened the whole pack. **Fixed:** `create_world` validates the
   pack loudly; the runtime swallow stays a safety net.

## Holistic cross-item review (2026-07-12) — all [AUTO] items landed

After the [AUTO] items shipped (B1, B2/D-34, B4, B5, B6, B3, small-fixes, the four landed-already
fixes), a **system-wide cross-item review** ran per the owner directive ("a final review on all
backlog holistically — redundancy or conflict between each backlog work"): 14 agents, five find
dimensions (read-surface drift · write-path conflict · port/boundary · trust/authority ·
fork/event-sourcing) → adversarial verify (default-REJECTED) → a completeness critic. **3 confirmed +
2 critic → 6 fixes; 0 in the highest-risk seams** (read-surface drift, counter-RMW TOCTOU, and the
`_SECTION_KEYS`/`_SNAPSHOT_TABLES` catalogs were all *verified* clean).

- **[MEDIUM, the real one] B3 REST × D-30 + Phase-3.** REST `create_campaign`/`join` pinned no
  ruleset (`ruleset_id=""`) and built no PC sheet — so a PbtA world's campaign started over REST
  silently fell to `uro-basic` with mechanics disabled, *and* the empty pin bypassed the WS
  cross-ruleset guard. **Fixed:** the REST path now reads `world_ruleset`, sheets the PC via the
  registry, and pins `ruleset_id/version` exactly like the CLI — REST and CLI campaigns no longer
  diverge.
- **[LOW] B3 × B5 read-endpoint 400 contract.** `GET /state?sections=<typo>` (via B5
  `query_across`) and `GET /chronicle?limit=abc` and `POST /time-skip {days<=0}` returned `500`,
  unlike every mutating endpoint. **Fixed:** bad input → `400` across the read surface.
- **[LOW, redundancy — the directive's headline] B1 × B6.** The server's outcome route hand-rolled
  `append_beat` + `react` — the exact forgotten-`react` footgun **B1's `append_and_react` exists to
  retire**. **Fixed:** it now calls `append_and_react`.
- **[hardening] B5 × projector.** An import-time assert now keeps `_SECTION_KEYS` (diff PKs) and
  `_SNAPSHOT_TABLES` (fork/query catalog) in lockstep, so a future projection can't drift them apart.
- **Verified-clean (no change):** `/roster` vs `/state?sections=pcs` read the same authoritative
  `proj_pcs` (no drift); `agenda_tick`'s window read is stale-safe because the `_react_lock` wraps
  the whole counter RMW; the concrete store matches the B3/B5 ports and no core-ring module imports
  the server or an adapter (the hexagonal contract is KEPT).

## The backlog (ranked by cross-game corroboration)

> **Triage (2026-07-12) — autonomous vs needs-owner-decision.** Working the backlog under a directive
> to clear everything that doesn't need an owner decision. Each item is tagged **[AUTO]** (shape is
> evidence-specified; build + review) or **[DECIDE]** (a genuine fork the owner should pick — flagged,
> not built). **AUTO:** B3 (endpoints enumerated), B4, B5 (API specified in Hollowloop G-5), B6
> *ingestion-receipt half*, and the P3 pure-fixes (loot to_ref liveness, `--token NAME=PARTICIPANT`,
> expose `campaign_pcs`, remove the dead `time_cost` field). **[DECIDE] (flagged below, not built):**
> B2 C3–C6 + computed-delta (owner staged as evidence-gated; OQ-1 defers computed-delta); B6
> *parked-encounter registry* (OQ-12 — waits for a real external game); B7 arbiter shapes (OQ-7 —
> UX/semantics design); B8 cross-fork memory (a never-forked lane — reverses fork semantics); B9
> deterministic planner path (approach choice); B10 session event-sourcing (reverses D-31 "turn state
> is session-only"); rumor statement-distortion (distortion model); branch GC (retention policy);
> pack thread-state vocabularies (grammar latitude); append-time emitter whitelist (repeatedly
> deferred as structural-at-source-suffices; hot-path risk). Dispositions annotated per item below.

### P0 — hit by 3–4 games, clear win

| # | Finding | Games | What Uro needs | Notes |
|---|---|---|---|---|
| B1 | **`append_beat` doesn't run the Reaction Layer** | **all 4** (Sable G-4, Ironwake, Seventh G-23, Hollow G-12) | ✅ **DONE** — `Engine.append_and_react(campaign, events) -> Commit` (commits + reacts in one exception-isolated call) | Small, unanimous. Was: an embedder who forgot the second `react()` call got silently dead rules. |
| B2 | **The computation / scripting tier (D-33 Stage B)** | **all 4** — Sable Court's **12-rule refusal log** (exact wished syntax) is the headline; Ironwake's 7, Seventh's alarm-meter, Hollowloop's RL-1 corroborate | ✅ **DONE (INC-C1+C2, D-34)** — engine-owned integer counters (`CounterChanged`→`proj_counters`, forking by construction — the shadow-state fix) + threshold/cross-entity-compare/count-edges + `world` scope; phase-end review passed (a counter-RMW concurrency lost-update fixed). WASM reserved at a sharper gate; C3–C6 (for_each/roll_table/expire/cascade) + computed-delta arithmetic staged (`docs/19`). | **The evidence gate we set for Stage B fired.** New argument: refused counters live in game code, so `fork_branch` no longer covers game state — **the two flagship features undermine each other** (Sable G-10, Ironwake row 6) — which is why the fix is *event-sourced* counters, not shadow state. |

### P1 — hit by 2 games, real blocker for a consumer class

| # | Finding | Games | What Uro needs |
|---|---|---|---|
| B3 | **No REST management/read surface** | Ironwake (row 1), Seventh Vault G-15 (10 enumerated 404s) | ✅ **DONE** — an authed management surface over the `EngineStore` port (docs/08 "What actually ships"): `POST/GET /worlds`, `POST /worlds/{w}/campaigns`, `GET /campaigns[/{c}]`, `POST /campaigns/{c}/join`, `GET /campaigns/{c}/{roster,state,chronicle}`, `POST …/time-skip` (`ServerDeps.store`; `501` transport-only; `400` malformed; `state` reuses B5 `query_across`). "The difference between *Uro has a server* and *Uro is a server*." Still CLI-only: seed/branches/probe/export/import, SSE beats, `/usage`; authority coarse (token authorizes, `participant` from body). |
| B4 | **Place-state never reaches the narrator** | Sable Court G-9 (with a recall dump), Hollowloop G-7 | A place channel in `RecallBundle` + the narrator prompt (+ entity-ref claim matching for `p:`/`f:` refs). A game about holdings can't let the narrator see holdings change. |
| B5 | **No cross-branch / aggregate query surface** | Hollowloop G-5 (937 ms to draw 502 loops — the *core UI* of a branching game is its slowest op), Sable Court (fork-compare) | ✅ **DONE** — `store.query_across(branch_ids, sections)` (ONE query per section via `branch_id = ANY(...)`, not N×round-trips) + `diff_branches(a, b)` (per-section added/removed/changed by PK). Read-only over `proj_*`. *Follow-up:* batching `world_time` across branches (still a per-branch CTE) — small P3. |
| B6 | **Chronicler write path returns no receipt / no protected-canon channel** | Ironwake (rows 2–3), Seventh G-22, Sable G-7 | (a) ✅ **DONE** — `distill_outcome_with_receipt` + the outcome endpoint return a per-ref receipt (`applied\|downgraded\|dropped` + reason); `distill_outcome` stays the events-only wrapper. (b) **[DECIDE]** the **parked-encounter registry** — pre-declared cast + an authorized channel for a protected actor's death ("kill the named boss") — stays deferred (OQ-12, waits for a real external game). |

### P2 — real, one primary consumer

| # | Finding | Game | What Uro needs |
|---|---|---|---|
| B7 | **Arbiter shapes beyond round-robin** | Seventh Vault S1 (5 concrete shapes) | ✅ **DONE (MVP, D-38)** — shipped the safe half: a structurally non-canon table-talk lane + `ProposalWindowArbiter` (propose-then-act, `AdmitDecision.QUEUED` now live) + `VoteArbiter` (consensus, an optional `VoteCoordinator` capability), all session-only (D-31), zero events/migrations/beat-loop change. CLI `uro serve --arbiter proposal\|vote` + `uro connect` `/say`//`/vote`. **Reserved behind the same port** (each verified to fit; validate-before-building on one game): consensual-PvP (edits the anti-grief invariant), simultaneous/composite (rewrites one-intent-one-beat), reactive/interrupt (needs the deferred `expected_head` guard — a 2nd concurrent writer), `take_pending`. |
| B8 | **Player-scoped knowledge that survives a fork** | Hollowloop G-1 (the genre-defining need) | ✅ **DONE (MVP, D-36)** — a caller-owned `ParticipantMemory` lane keyed `(participant_id, world_ref)` (migration 017), deliberately NOT a projection/snapshot so `fork_branch` never resets it (fork-survival structural); surfaces in the narrator prompt as the player's private recollection; caller key + `sha256` dedup (G-2). CLI `uro codex add/list`. The event-sourced journal (audit/forget) + a separate participant export are reserved behind the same port on an evidence gate. |
| B9 | **Deterministic path into the mechanics gate** | Seventh Vault G-1 | ✅ **DONE (MVP, D-37)** — `Engine.run_beat/run_beat_stream/preview_beat(..., plan=BeatPlan)` injects a caller-supplied plan and skips the LLM planner, so CI + keyless consumers resolve free-roam checks AND full encounters deterministically. The supplied plan is fenced by the SAME `validate_plan` as an LLM plan (unknown affordance → `PlannerError`); it is a TRUSTED in-process input (no D-32 ceiling — that fences the external Chronicler POST; a future network-exposed `plan=` MUST add it). `BeatResult.checks` now carries `check_traces` (per-check detail, incl. the resolved fight's rounds). Reserved behind #8: a rule-based intent→affordance fallback planner + a `CheckResolved` event trace; not yet wired to `serve`/CLI. |
| B10 | **Session lifecycle isn't event-sourced** | Seventh Vault G-18/G-19 | ✅ **DONE (MVP, D-39 — REFINE D-31, not reverse)**. G-18: `store.pc_seats` derives the arbiter ring ORDER from the already-event-sourced PCBound/PCReleased log, so reconnect/restart re-forms the SAME order (no new turn state — the cursor stays session-only; a durable cursor would ride `fork_branch`, so it's refused). G-19: a durable, hashed, revocable, **campaign-scoped** `session_tokens` registry (migration 018, off the branch axis) behind the single `resolve_participant` choke point — a late joiner mints a live token via the authed `/join` (no restart). CLI `--admin-token` (operator tier, distinct from player `--token`), `--arbiter` decoupled from token count, `uro token mint/revoke`. **Deferred (named):** resume-after-crash (cursor resets over a stable order); a per-message revoke re-check (revoke blocks reconnect, not a live socket); per-participant token cap / revoke-on-end; cross-process cache coherence. |
| B11 | **`append_and_react` gap's cousins** — `create_thread`/`add_edge` scope is single-dimension | Sable G-3/G-12, Ironwake, Seventh G-6 | A `world` scope + multi-ref scopes (`{factions:[a,b]}`); a dropped-action audit trail (actions silently vanish today). |

### P3 — smaller / documentation / ergonomics

> **G-3 — beat RNG's flaky-gate root cause — ✅ DONE** (Seventh Vault G-3; discovered during the B4
> build). `_beat_rng` hashed the fresh-per-run `campaign_id : head_commit` (both `new_id()`), so the
> mechanics RNG differed **even on a replay of the same event log** — a guaranteed-loss fight would
> occasionally flip (a flaky gate). **Fixed:** `_beat_rng` now derives from the campaign's
> **persisted seed** + the commit **depth** (both deterministic, no per-run id). The seed lives on
> `CampaignStarted` and is denormalized to `campaigns.seed` (migration 016, `DEFAULT 0` so
> pre-existing campaigns stay valid); `start_campaign` writes it, `get_campaign`/`list_campaigns`
> load it, `Campaign.seed` carries it; `uro campaign new --seed` (and REST `create_campaign` body
> `seed`) pin it (validated to int64). No re-pin storm: the acceptances are seed-INVARIANT (PC loses
> across all seeds) so all 34 integration combat tests passed unchanged; `tests/test_beat_rng.py`
> proves the property.
>
> **Honest scope (don't overclaim):** the fix makes the **RNG stream** a pure function of
> `(seed, depth)` — it does *not* make the whole combat *outcome* a function of only those (the
> outcome also depends on the sheets/combatants the beat feeds the ruleset). A fight therefore
> replays identically **given an identical event log up to it** — which holds under a deterministic
> provider (the stub, so the **CI gate is now reproducible — G-3's actual target**) or on replay,
> but **not necessarily across live-LLM runs**, where the extractor/planner can vary the events a
> beat emits (and whether `react()` appends a module beat), shifting `depth` and even whether/where
> a fight triggers. Committed *events* also still carry random ids, so it is not byte-identical-event
> replay through the engine (the `run_encounter` UNIT, given a fixed `encounter_id`, already was).

Item-transfer from a free-roam beat (Ironwake, Seventh G-7, Hollow G-8 — no non-encounter effect
channel) · game↔world **time mapping** + `time_cost` is a dead field + once-per-skip agenda
semantics (all 4) · `AliasAdded` / actor-merge for post-hoc entity repair (Sable G-5) · loot
`to_ref` liveness not checked (Ironwake) · rumor **statement** distortion, not just confidence decay
(Ironwake, "legend grows in the telling") · pack-declared thread-state vocabularies (the closed
`ThreadState` literal blocks a game's own alarm words — Seventh G-5, Hollow G-6) · branch deletion /
GC (Hollowloop G-11) · `--token NAME=PARTICIPANT` + expose `campaign_pcs` in the read surface ·
`tier` capped at 3 vs a brief's `tier 4` (doc mismatch, Sable G-2) · snapshot cadence is depth-from-
genesis so it never fires in a fork-per-loop game (Hollowloop G-4) · append-time emitter whitelist
(the by-policy invariant; Ironwake, Seventh G-27 tripped it deliberately).

## Validated DEFERRALS — what NOT to build (equally valuable)

The games proved several horizon items are **not** needed, killing speculative work:

- **A specialized graph/vector store is NOT justified.** Postgres-as-graph held **flat to 500
  forks** (fork latency O(origin state), not O(branches); Hollowloop measured it). The one real cost
  was a missing index (B-fixed), not the store. *Contra the earlier assumption that scale would force
  a swap.*
- **The `entity_index` (OQ-3) is NOT justified.** Canonical-name + alias resolution held for a court
  of dozens with **0 false merges** (Sable Court), given authored aliases. The cheap missing piece
  is `AliasAdded` for post-hoc repair, not embeddings.
- **The protection ceiling is a *feature*, not a defect.** "The man you killed stands on the
  palisade" was Ironwake's best scene; it emerged *from* the trust model. Keep the blanket ceiling;
  add only an authorized channel (B6) for the "kill the boss" genre.
- **Zero-witness silence, idempotent bundles, the in-process party-race** — all confirmed
  done-and-good. No work needed.
- **NATS distribution** — no consumer is remotely near multi-process scale; still correctly deferred.

## The headline decision: Stage B's evidence gate has fired

D-33 reserved the WASM scripting tier and **defined its trigger**: *"refused until a documented
refusal-log shows authors genuinely need computation the data DSL cannot express."* That log now
exists, four times over — most sharply as Sable Court's 12 wished-for rules in exact pack syntax.
And a stronger argument than we anticipated surfaced independently in two games: because refused
counters fall into game code, they **fall out of `fork_branch`** — so Uro's declarative-rules
feature and its branching feature actively undermine each other through this hole. The question
"should we build Stage B?" is now answerable with data. **Recommended next decision:** scope
engine-owned numeric state / the computation tier (a new decision superseding the D-33 reservation),
using this refusal log as the spec.
