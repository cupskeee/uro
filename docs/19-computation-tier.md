# 19 — The Computation Layer (engine-owned numeric state) — PROPOSAL

> **STATUS: PROPOSAL, not accepted.** Nothing built; no `D-34` in `decisions.md` yet. Output of the
> 2026-07-12 design pass (ground → 4 approaches → 3 judge lenses → synthesize → risk critic),
> triggered because the **D-33 Stage-B evidence gate fired** (all four games in `examples/games/`
> produced refusal logs — see `docs/18`). If accepted it supersedes one D-33 clause as **D-34**.
> React to it — especially the **honest coverage boundary** and the open questions — before any code.

## The problem the games proved

D-33 shipped a *declarative* Reaction Layer and deliberately refused numbers ("no counters,
arithmetic, or accumulating state"), reserving a WASM scripting tier until a refusal log proved
authors need computation. **That gate fired four times.** The union of what the games needed is
**bounded integer accumulation** — counters + threshold + reset is the #1 refusal in *all four*
(Sable RL-1 tension→war, Ironwake win/kill counts, Seventh's alarm heat-meter, Hollowloop's
dread-after-3-visits) — **not** Turing-completeness.

And the decisive argument is **structural, not ergonomic**: because the grammar refused numbers,
every game put its numbers in *shadow game-code*, and shadow state **does not ride `fork_branch`**
(Sable G-10, Ironwake row 6). So Uro's signature feature (branching) stopped covering the whole game
state, and **the two flagship features undermine each other.** The only fix that satisfies that
constraint is to make numbers **event-sourced engine state** that forks by construction.

## The verdict: an engine-owned Computation Layer, not WASM

Four approaches, judged on determinism-&-fork-consistency / trust / coverage-per-effort /
composition:

| Approach | Avg | Verdict |
|---|---|---|
| **Computation Layer** (staged engine counters + a small closed grammar) | **8.33** | strong ×3 |
| **Ledger Tier** (engine integer counters) — *converges on the same core* | 8.00 | strong ×3 |
| Uro Formula (a full pure-expression DSL) | 6.67 | viable ×3 |
| WASM Sandboxed Proposer (D-33 Stage B) | 5.33 | weak/viable |

The two counter-based designs converge on the same **INC-C1 core** and win decisively. **WASM is
rejected as the first move** for three verified reasons: (1) its own coverage analysis concedes the
#1 refusal doesn't need it; (2) it re-introduces the from-scratch native sandbox D-33 *dissolved*;
(3) under one-process/multi-campaign embedding, a wasmtime host-escape is a cross-tenant breach. The
full-expression DSL is an XL monolith that front-loads the whole risky surface where the staged
counter tier ships the common 80% first. **Keep the WASM port reserved, at a sharper gate.**

## The design (staged)

**State + event model (the load-bearing decision — verified correct by the critic).** A new closed
event `CounterChanged{scope_ref, key, to_value:int, created_day, updated_day, caused_by=module_cause}`
— **absolute value, never a delta** (a delta double-counts on replay/re-POST/cascade; absolute-set
is idempotent-by-UPSERT, following the `BeliefChanged`/`distill_outcome` bake-the-result precedent).
Projected to `proj_counters` (migration 015), registered in **both** `projector._HANDLERS` **and**
`_SNAPSHOT_TABLES` — the two-and-only-two fork registration points — so it **forks / replays /
snapshots / exports for free, with zero new fork code**. Integer (`BIGINT`) only. `get_counter` /
`list_counters` join the `ProjectionQueries` port. *This deletes the `copy.deepcopy(ledger)` shadow
state that broke branching — the whole point.*

**Grammar additions** (purely additive to the two existing closed Pydantic unions; no eval/parser;
YAML→validated Pydantic; bumps `RULES_API_VERSION` 1→2). Conditions: `counter` (threshold),
`counter_compare` (integer cross-multiply, so `strength(A) > strength(B)*1.2` → `left*10 > right*12`,
no float), `count_set` / `count_edges` (bounded). Actions (→ `CounterChanged`, absolute-baked):
`set_counter`, `adjust_counter` (bounded `±delta`), `reset_counter`; later `roll_table` (seeded),
`for_each` (the ONE bounded loop, fan-out + node-budget capped), `expire_claim`.

**Trust (the fence goes from vacuous to ACTIVE).** `CounterChanged` writes *only* `proj_counters` —
structurally it can never become a sheet/hp/item/death or a `truth=true` claim. Reached only through
the closed Action union (the "no author code" property is intact — a counter is bookkeeping *data*).
Scope-fenced on **reads and writes**. Mandatory magnitude cap `_MAX_COUNTER`, fail-closed, **shipped
in C1, not later**. Per-actor counters deferred (a faction's strength is fine; grinding a T2+ actor's
meter waits behind `_is_protected`). *(Critic: the counter-as-hp-proxy risk is already structural —
rulesets receive only an opaque `Sheet` + seeded RNG; the guard is simply "never plumb `get_counter`
into the ruleset," not a policy to police.)*

**Determinism.** All arithmetic runs once at live-play in trusted in-ring code; the **absolute
result is baked** into the event; replay only re-UPSERTs (never re-evaluates). No wall-clock, no
ambient random. `roll_table` seeds from committed state via **explicit integer-hash selection** (not
`random.choice`), so it beats the CPython-version caveat; the resolved outcome is baked, replay never
re-rolls. *(Critic framing fix: integer-only is for cross-platform arithmetic determinism; and
"byte-identical forever" is a **replay** guarantee — the live roll is deterministic-given-inputs,
exactly the `distill_outcome` precedent.)*

**Cascade (RL-12), last + highest-risk.** Today `react()` is single-hop by construction. INC-C6 adds
a bounded, deterministic, fail-closed re-entry (`_MAX_CASCADE_HOPS`) so a counter crossing a
threshold can fire a second rule in the same pass — requires making `CounterChanged` triggerable and
ships with a **dedicated system-wide cross-phase review** (cascade × fork-replay × export × party).

## The honest coverage boundary (critic headline — read this)

The bounded counter tier covers **fixed-amount** accumulation: `tension += 1`, `heat += 1`,
`influence += 1`, threshold gates, resets, cross-counter *comparison*. It does **NOT** cover
**computed** arithmetic where the delta is a function of other state — RL-2's economy
(`gold = gold + sum(holding_value) − upkeep*strength`) and RL-6 (`strength += gold//10; gold %= 10`).
`adjust_counter` takes a *literal* delta, not an expression over counters. So:

- **Deleted shadow state:** the counter/threshold/meter half (the #1 refusal, all four games). ✅
- **NOT deleted yet:** the *economy-formula* half of Sable Court's ledger. It needs either a small
  bounded expression in `adjust_counter` (`delta = a*counter − b*counter`) — a candidate later
  increment — or it becomes the **sharper next evidence gate** for the reserved tier. This must be
  stated plainly; the "delete all shadow state" claim is currently **partial**.

Other critic corrections folded into the plan: `Rule.when` gates *all* of `then`, so
"increment-always + edge-if-threshold" is a **two-rule, one-beat-lag** pattern until cascade (C6)
makes it same-pass — not atomic in C1 (a fidelity note, not a blocker). Two `adjust_counter` on the
same key in one pass **must accumulate in-pass** (a naive read-then-bake drops one increment — a
real correctness must-fix in C1). RL-8 `expire_claim` has an **unstated dependency**: a `created_day`
on `proj_claims`/`ClaimRecorded` (its own migration). RL-5 fall-of-house should use
**`count_edges`** (reusing `edges_from` over `owns`), not per-member counters. And out-of-scope drops
/ magnitude saturation must emit a **dropped-action audit trail** (the no-diagnostics footgun the
games flagged) — ship it *with* the numbers.

## Staged build sequence

1. **INC-C1 (L — the core + the shadow-state fix, MUST):** migration 015 + `CounterChanged` +
   `_counter_changed` handler + `_SNAPSHOT_TABLES` entry + `get_counter`/`list_counters` +
   `CondCounter` + `set/adjust/reset` + **in-pass accumulation** + mandatory `_MAX_COUNTER`
   (fail-closed) + a **dropped/clamped audit trail** + `RULES_API_VERSION` 1→2 + docs/12 catalog +
   emitter-whitelist (M). Wired at both hooks. **Acceptance MUST fork past a snapshot boundary and
   assert the counter survives byte-identically**, + a meteor-with-counter replay test. Covers RL-1
   (all four), single-entity fixed accumulation.
2. **INC-C2 (M — co-requisite pair):** the multi-ref / `world` scope generalization (B11 — a
   cross-entity counter has *no* single-dimension jurisdiction today, so this is a hard co-requisite,
   not optional) + `counter_compare` + `count_set`/`count_edges`. Covers RL-3, RL-5.
3. **INC-C3 (M):** `for_each` (one bounded loop, capped) + `$trigger.<field>` binding (validated like
   `_trigger_can_fire`) + edge traversal, each neighbor scope-fenced. Covers RL-11 (single-hop).
4. **INC-C4 (S):** `roll_table` (seeded integer-hash, baked). Covers RL-4, Seventh RL-6.
5. **INC-C5 (S):** `expire_claim` (+ its `created_day`-on-claims migration). Covers RL-8.
6. **INC-C6 (M, HIGHEST-RISK, LAST, dedicated cross-phase review):** bounded fail-closed cascade
   re-entry + make `CounterChanged` triggerable. Covers RL-12.
7. **(Undecided) computed-delta arithmetic** — the economy-formula half. Either a bounded expression
   in `adjust_counter`, or defer to the sharper reserved-tier gate. **Owner decision (OQ below).**

## What to NOT build

WASM/Stage-B now (keep `ports/module.py` reserved). Float / general recursion / user functions /
unbounded loops / eval / a parser. Delta-valued counter events (double-count). A side store or
in-memory counter cache (that *is* the shadow-state bug). A module constructing a raw `DomainEvent`
(evaporates the structural-at-source whitelist). Cross-entity compare without the multi-ref scope (a
scope-escape hole). The magnitude cap "later" (mandatory in C1). Touching `react()`'s single-hop
invariant before C6.

## Open questions (decide before building)

1. **Computed-delta arithmetic (the economy-formula half):** a bounded expression in
   `adjust_counter` now, or defer it as the reserved-tier's sharper gate? (This decides whether the
   Sable economy shadow-state is deleted or explicitly deferred.)
2. **The multi-ref / `world` scope shape** (B11): list-of-refs vs a `world` wildcard vs a pair-scope
   — the sharpest scope-escape surface; wants its own mini design pass before C2.
3. **Materialize-at-trigger-commit read pin:** accumulation makes read-at-commit *load-bearing*
   under P7 party play. Pin it structurally when C1 lands, or accept round-robin serialization?
4. **Downtime economy under a multi-boundary skip:** add a `boundaries_crossed` binding to the
   agenda context, or keep explicit-multiplier authoring discipline?
5. **The reserved-tier gate wording (D-34):** WASM opens only on a NEW refusal log showing the
   bounded primitives were *structurally* (not verbosely) insufficient — is that testable as stated?

## Draft decision (proposed — appended only on acceptance)

> **D-34 — Pack-authored NUMERIC state = an engine-owned, event-sourced Computation Layer (bounded
> integer counters as typed `CounterChanged` events → `proj_counters`, forking by construction via
> `_HANDLERS` + `_SNAPSHOT_TABLES`); the D-33 Stage-B WASM port stays RESERVED at a sharper,
> structural-insufficiency gate. Supersedes the D-33 clause "genuine computation is refused."** The
> refusal-log gate fired (four games); the need is bounded integer accumulation, not
> Turing-completeness, and the only fork-safe home for it is event-sourced engine state (shadow state
> breaks branching). Grammar gains closed variants (no eval/float/recursion/user-functions; one
> bounded, budget-capped loop). Absolute-baked values → deterministic replay. Trust fence goes from
> vacuous to active (counters are structurally non-canon, scope-fenced, magnitude-capped, non-actor
> scoped). `RULES_API_VERSION` 1→2. Honest boundary: fixed-amount accumulation lands; **computed
> cross-counter arithmetic (economy formulas) is explicitly out of the first tier** (OQ-1). WASM
> re-affirmed reserved: opens only when a refusal log shows `_MAX_FANOUT`/`_MAX_NODES` were tripped
> (transitive closure / data-dependent unbounded iteration / computed weight tables), which today's
> logs do not clear.
