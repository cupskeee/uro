# The Sable Court

A **court-intrigue realm simulation** built ON the Uro engine — _Crusader Kings_-style Houses
that feud, marry, and go to war as a living, **forkable** chronicle. You are the Spymaster of
Karsis: the King is dying without an heir, and you nudge the realm with whispers, forged
letters, brokered marriages, and one knife in the dark — then fork the timeline and watch the
history where you sued for peace diverge from the one where you didn't.

This game is also a **forcing function**: it deliberately pushes Uro's declarative Reaction
Layer and its world-simulation seams until they refuse, and logs every refusal at the call
site. The scientific output is [`GAP_REPORT.md`](GAP_REPORT.md) — headline: a 12-entry
**refusal log** of realm rules the rule grammar cannot express (the evidence gate for the
reserved WASM scripting tier, D-33 Stage B).

## Run it

```sh
docker compose up -d --wait        # Postgres + pgvector on HOST PORT 5433
uv run uro db migrate
uv run python examples/games/sable-court/sable_court.py
```

**No API key.** The default provider is a deterministic scripted one (court prose + extraction
served per beat) and the realm's war dice come from one frozen seed — a clean-DB re-run is
**byte-identical**, and the run self-verifies with 46 printed assertions. A real model is
opt-in: `--provider openai|anthropic [--model …]` (needs a key in env; assertions relax to
observations, since live prose extracts differently).

## What one run plays

- **Stage 0–1** — the engine comes up (Posture A: `uro_core` embedded in-process); the realm of
  Karsis is seeded as Uro canon: 18 actors (with deliberately confusable names — Aldric /
  Aldrice / Aldric the Younger, Ser Garret / Garrick / Gareth — resolved via authored aliases),
  7 factions, 7 owned holdings, 12 plots, a `knows` gossip web, and the Reaction-Layer rule pack.
- **Stage 2** — eight intrigue beats through `engine.run_beat` (whisper, investigate, blackmail,
  bribe, sell-secret, incite-feud, broker-marriage, interrogate); the brokered alliance fires
  post-beat rules (a counter-plot thread is created, a guild rumor lands `truth=unknown`).
- **Stage 3** — downtime: `engine.agenda_tick` fires pack agendas (war rumors, spreading heresy)
  in lockstep with the game's **shadow ledger** (House gold/strength/tension — the numeric sim
  Uro structurally cannot own). Tension boils into war; battles resolve numerically and are
  reported through the **Chronicler** (`distill_outcome`): a T0 levy really dies (and wakes the
  feud thread via a rule), while the Marshal's "death" and the King's assassination are
  **downgraded to rumors** by the trust ceiling — asserted, on purpose. A zero-witness ambush
  propagates nothing.
- **Stage 4** — scale stress: threads grow to 27 (18 live plots flood every narrator prompt);
  the Border March changes hands and a recall dump proves the narrator is **blind to
  place-state**; the one unaliased handle ("the Salt Knight") fragments into a new actor.
- **Stage 5** — the signature: `fork_branch` at the war's first blood → on the fork the
  Spymaster brokers the white peace → divergent downtime → the two lines demonstrably disagree
  on war edges, thread states, rumor sets, and who is dead; both replay cleanly.
- **Stage 6–7** — the refusal log (12 wished-for rules the grammar refused) and the friction
  log (12 gaps with receipts) print with the run.

## Uro surface exercised

**Posture A** (embedded `uro_core`, async) · `create_world(extra_events, rule_pack)` ·
`start_campaign` (fresh PC + adopt-on-fork) · `run_beat` / `preview_beat` · the **Reaction
Layer** (post-beat rules + downtime agendas, scope fence, silent-drop behavior) ·
`agenda_tick` / `time_skip` · the **Chronicler** (`OutcomeBundle` → `distill_outcome`,
protection ceiling, participant scope, loot ownership, zero-witness silence) · belief
propagation along `knows` edges with confidence decay · `append_beat` (authored events) +
manual `engine.react` · `fork_branch` + divergent replay · projections (`list_actors`,
`find_actor_by_name` alias resolution, `claims_about`, `beliefs_of`, `list_threads`,
`list_edges`, `get_place`, `get_item`, `current_world_time`) · `assemble_recall` (as evidence).

## Headline finding

The declarative Reaction Layer carried everything *qualitative* (thread flips, rumors, edges,
scheduled events) but **none of the numeric realm** — no counters, arithmetic, loops, or
tables — so the whole House simulation lives in game code as a shadow ledger… which then
**breaks the engine's signature feature from the outside**: `fork_branch` forks every Uro
projection perfectly, but the game must manually snapshot/restore its own numbers at the exact
fork commit. State the engine owns forks for free; state it refuses to own silently stops
forking. That — plus the trust ceiling making a game about assassinating great lords unable to
ever kill one — is the case for the reserved engine-owned computation tier. Full receipts in
[`GAP_REPORT.md`](GAP_REPORT.md).

## Files

| file | role |
|---|---|
| `sable_court.py` | entry point — the staged, self-verifying session |
| `realm.py` | the authored realm: cast/geography/plots + the `rule_pack` |
| `ledger.py` | the shadow ledger (the numeric sim Uro can't own) + its refusal log |
| `script.py` | the deterministic scripted provider + court prose |
| `frictionlog.py` | call-site gap/refusal collectors (printed every run) |
| `GAP_REPORT.md` | the scientific output |
