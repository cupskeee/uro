# Live thesis run — OpenAI

Runbook for the Phase 1 thesis validation (`10-roadmap.md`, T1/T2) against a real model —
the one experiment the build environment couldn't run (no key). OpenAI is the simplest
path: **one key does everything** — chat (narration + extraction) *and* embeddings
(semantic recall).

## 1. Setup

```sh
export OPENAI_API_KEY=sk-...          # one key, full stack
docker compose up -d --wait           # Postgres + pgvector (host port 5433)
uv run uro db migrate                 # if not already applied
```

With `--provider openai` the wiring binds **gpt-4o-mini** to narration + extraction and
**text-embedding-3-small** to the embedder role automatically. Upgrade the narrator with
`--model gpt-4o` for richer prose (same key); the embedder stays an embedding model
regardless. Override the embedding model with `URO_EMBED_MODEL` if you like.

Note: Phase 1 has **no world packs yet** (that's Phase 4) — the setting is baked into the
narrator prompt (a tavern). Scenes will be tavern-flavored. Fine for the thesis test.

## 2. Smoke + verify extraction actually works

The reviews flagged several *silent* extraction-failure modes (all fixed); the first real
run is exactly when to confirm state is genuinely being built.

```sh
uv run uro world new "Live Smoke"     # → a campaign id; call it $S
uv run uro play $S --provider openai
#   > I ask the barkeep, Mera, what she knows about the missing dockworker
#   > I press her on who she suspects and why
#   /quit
uv run uro consistency $S             # total > 0  ⇒  the extractor produced narrator claims
```

If `consistency` prints `0/0`, extraction produced nothing — check the narration actually
named people / asserted facts (the extractor only records what the prose *states*). For a
direct look at the state that got built:

```sh
docker compose exec postgres psql -U uro -d uro -c \
  "SELECT (SELECT count(*) FROM proj_actors) actors, (SELECT count(*) FROM proj_claims) claims, (SELECT count(*) FROM memory_index) memories;"
```

Nonzero actors/claims/memories = the full pipeline (recall → narrate → extract → gauntlet
→ project → embed) is working end-to-end against the real model. Extraction/embedding
failures log a **warning to stderr** (Python's default), so you'll see them scroll by; a
committed beat is never lost to a downstream failure.

## 3. The ablation experiment (T1 — the real thesis signal)

The bet: state-tracked narration beats a raw transcript. Play the **same intents** through
two **fresh** campaigns — full engine vs `--bare` (no state/recall/extraction/memory) —
and compare. Use fresh campaigns; don't reuse the smoke one.

**Shortcut:** `bash scripts/ablation.sh` (or `MODEL=gpt-4o bash scripts/ablation.sh`) does
exactly the steps below with a 14-intent script designed to plant early and reference late.
The manual version:

```sh
uv run uro world new "Ablation FULL"          # → $A
uv run uro world new "Ablation BARE"          # → $B
uv run uro play $A --provider openai          # full engine
uv run uro play $B --provider openai --bare   # raw-transcript GM
```

Type the **same ~20–30 intents** into both — keep a written list so both arms get
identical input. Design them to plant something early and reference it late:

- **beats 1–3:** introduce a named NPC and a fact ("Mera tells me the Duke disbanded his army after the peace").
- **beats 4–12:** wander, meet other people, do unrelated things — push *past* the 8-beat recency window.
- **beats 13+:** return and reference the early NPC/fact ("I find Mera again — does the Duke's peace still hold?"; "does anyone here dispute what she told me?").

**The signal is at beats 13+, past the recency window:**
- **Full (`$A`):** structured + semantic recall re-surface Mera and the Duke fact; the narrator stays consistent and a knowledgeable NPC can correct a contradiction.
- **Bare (`$B`):** only the last 8 beats are in context; Mera and the early fact have scrolled out — expect drift, forgotten names, contradictions.

If `$B` is indistinguishable from `$A`, that's the **kill signal** (`10-roadmap.md`) —
cheaper to learn now than after building Phase 2 on top. Read both transcripts yourself
(they're in the event log: `uro log` isn't built yet, but `psql … SELECT payload FROM
events WHERE event_type='BeatResolved'` dumps them).

## 4. T2 metric

```sh
uv run uro consistency $A
```

It's a **proxy** (`10-roadmap.md`, review inc 4): it only catches contradictions the
extractor self-flagged against recalled state, so a good model reads high by construction.
Use it as a regression trend across changes, not as absolute proof.

## 5. Cost & gotchas

- ~30 beats × (narrate + extract + 2 embeds) at gpt-4o-mini + text-embedding-3-small is
  roughly **cents**. `--model gpt-4o` costs more but narrates noticeably better — worth it
  for a fair thesis test.
- **Don't mix providers on one campaign's DB.** The stub (256-dim) and OpenAI (1536-dim)
  embeddings collide; use a fresh campaign per provider. (Search degrades gracefully if
  they do collide — you just lose semantic recall for that campaign.)
- A missing key prints `error: OPENAI_API_KEY is not set`, not a traceback; a nonexistent
  campaign prints a clean `no such campaign`.
- gpt-4o-mini follows the extractor's strict-JSON instruction well; if you swap in a weaker
  model and `consistency` shows `0/0` on beats that clearly asserted facts, the extractor
  JSON is likely failing to parse (watch stderr for the "not parseable JSON" warning).

## 6. Post-PoC live validation (Phases 6–8)

The post-PoC phases (alien ruleset, multiplayer, Chronicler hardening) are proven only by
**deterministic** tests — CI never calls a live model. This exercises the LLM-in-the-loop legs
with a real one; the owner runs it, Claude analyzes from Postgres.

```sh
export OPENAI_API_KEY=...              # (or MODEL=gpt-4o for sharper prose)
bash scripts/postpoc_validate.sh
```

- **Leg A — Phase 6 (uro_pbta).** Creates an Emberfell campaign bound to the PbtA ruleset and
  plays ~8 intents live. What to look for: the LIVE planner picks PbtA **moves** (not d20 verbs);
  an aggressive intent triggers a **2d6 conflict**; the narrator weaves 7-9 *partial* outcomes.
  Analyze: `campaigns.ruleset_id = 'uro-pbta'`; the PC sheet in `proj_sheets` carries
  `stats`/`harm`/`conditions` and **no** `hp`/`ac`; a conflict commits `EncounterStarted` +
  `EncounterTurnTaken.result ∈ {miss,partial,full}`; claims/beliefs built from real narration.
- **Leg B — Phase 8 (Chronicler).** Seeds an external toy battle whose feat becomes a witness
  rumor (`scripts/warstory_live.py`, deterministic), then asks a **live** narrator to retell it.
  What to look for: the third-hand, low-confidence belief surfaces as a **hedged rumor** in real
  prose, not settled fact. Analyze: the feat is `truth=unknown`/`origin=external`; Mera's belief
  confidence is low; the Leg-B transcript in `events` retells it hedged. (The scope/protection
  itself is deterministic — see `tests/test_chronicler_hardening.py`.)
- **Phase 7 (multiplayer) — manual, two terminals.** Round-robin arbitration is deterministic
  (tested), but to see it live: `uro campaign new <world> --pc Ash`, `uro campaign join <cid>
  --participant player-2 --pc Bane`, then `uro serve --token tok-a --token tok-b` in one
  terminal and `uro connect <cid> --token tok-a` / `--token tok-b` in two others. Only the
  turn-holder's intent runs; the other sees `not your turn`; the token rotates each committed beat.

Analysis queries (Claude, no key): `docker compose exec postgres psql -U uro -d uro` then e.g.
`SELECT ruleset_id FROM campaigns WHERE campaign_id='<id>';`, `SELECT sheet FROM proj_sheets
WHERE actor_id='a:ash';`, `SELECT event_type,payload FROM events e JOIN commits c ON
c.commit_id=e.commit_id JOIN branches b ON b.world_id=c.world_id WHERE b.branch_id='<branch>'
ORDER BY c.depth, e.seq;`.

## 7. Extraction tuning — replay recorded narration (D-51)

To judge an extractor change (prompt, context, gauntlet) without the narrator changing the story
too, replay a played world's **recorded** narrations through the current extractor:

```bash
uv run python scripts/extraction_replay.py Solara --provider stub   # dry-run, no key
uv run python scripts/extraction_replay.py Solara                   # the registry's extractor
```

Each beat replays in order onto a `replay-<id>` fork of the source world, taken just before its
first beat, so the extractor starts from what the original saw (the pack's cast, the PC, a seeded
rumor and who believes it): recall is assembled as live (recent beats + state so far), the
extractor runs on the recorded prose, the gauntlet commits. It prints what each beat committed and
a summary beside the original run (entities, threads, edges, claims, exact claim repeats). Live mode uses the `extractor` role binding of the model-connection
registry (D-47) — one call per beat plus re-asks, no narrator calls — and needs `URO_SECRET_KEY`
(auto-loaded from `.env`). Reworded duplicates (one plot, several phrasings) aren't counted
automatically: read the per-beat thread lines, or query the replay branch in Postgres. The source
can be an id or a name (an ambiguous name is refused with the ids).

To exercise the relational path (factions, `member_of`/`located_in`, edges) a story has to name
groups and places. `scripts/relational_live.py` plays a fixed 15-intent session that steers the
narrator there (a walled town, the order keeping its peace, a rival group, a shared trouble asked
about three ways) on the registry's models, then prints each beat and a summary; replay its world
afterwards to A/B a later extractor change on the same prose (`--provider stub` dry-runs it).
`--scenario resolution` instead sets up a plot (a missing girl) and then settles it, to check that
the extractor resolves the thread (D-52) at the beat the story ends it — and not before.

## 8. Server, live commits, metering, backfill, probe, war story (v0.7)

The pass first run on 2026-10-31 (results: `docs/16`, "the v0.7 live run"). Setup:

```sh
docker compose --profile nats up -d --wait     # Postgres + NATS (bound to localhost)
uv run uro db migrate
uv run uro provider test <chat-connection-id> --model <model>    # each should print ok:
```

**Leg 1 — the server with real models + live commits** (registry models):

```sh
uv run uro outbox relay --nats nats://localhost:4222                     # terminal 1
uv run uro world create worlds/ashfall                                   # → <W>
uv run uro campaign new <W> --pc Ash                                     # → <C>
uv run uro serve --nats nats://localhost:4222 --token tok-a              # terminal 2
uv run uro connect <C> --token tok-a                                     # terminal 3: play ~10 intents
uv run uro campaign time-skip <C> --days 2                               # terminal 4 → 3 prints "the world changed"
uv run uro serve --port 8001 --nats nats://localhost:4222 --token tok-b  # terminal 4
uv run uro connect <C> --server http://127.0.0.1:8001 --token tok-b      # terminal 5: one beat → shows in 3
```

**Leg 2 — backfill and probe** (`export OPENAI_API_KEY=…`): `uv run uro world probe worlds/ashfall
--provider openai`, then `uv run uro world create worlds/thornwood --backfill --provider openai`.

**Leg 3 — a rumor retold live:** `uv run python scripts/warstory_live.py` (→ `CAMPAIGN <S>`), then
`uv run uro play <S> --provider openai --no-mechanics` and ask Mera what news there is, what she has
heard of a wizard on the road, and whether she believes it.

**Leg 4 — a fixed session to replay later:** `uv run python scripts/relational_live.py`.

Read back: `uro usage --world <W>`, `uro calls --stage extractor --campaign <C>` (a failed call
shows `FAILED:` and why), the claims and beliefs each beat committed, and Mera's belief in the
feat (`proj_beliefs`) — it should stay low and keep its `learned_from` after she retells it.

## 9. Second pass: a party, Uro Pool, world rules, a cheaper extractor (v0.8)

What the first pass (§8) didn't cover. Postgres must be up; NATS isn't needed.

**Leg A — a two-player party on Ashfall, with a world rule.** Ashfall's `failures-get-talked-about`
rule turns a failed check into a rumor at the Brinehouse.

```sh
uv run uro world create worlds/ashfall                        # → <W>
uv run uro campaign new <W> --pc Ash                          # → <C> (player-1)
uv run uro campaign join <C> --participant player-2 --pc Bane
uv run uro serve --token tok-a --token tok-b                  # terminal 1: two tokens → round-robin
uv run uro connect <C> --token tok-a                          # terminal 2 (player-1)
uv run uro connect <C> --token tok-b                          # terminal 3 (player-2)
```

Take turns. While one player's beat is streaming, type the SAME intent in the other window: both
should print "not your turn", and the first player's prompt must wait for its own ✓. Make two or
three risky attempts (a lock, a guarded door) so a check fails, then ask Mera at the Brinehouse
what people are saying. `/say` sends table talk that never commits.

**Leg B — Uro Pool on Brinehold** (the same server hosts every ruleset):

```sh
uv run uro world create worlds/brinehold                      # → <W2>
uv run uro campaign new <W2> --pc Wren                        # → <C2>
uv run uro connect <C2> --token tok-a
```

Talk to Maren Coil (harbour warden), Tobin Reave (salvage captain) and Ilse (net-mender); try
something risky and push it; pick a fight over the Drowned Barque's cargo.

**Leg C — the extractor on `gpt-4o-mini`.** Replay two recorded sessions with the extractor bound
to the cheaper model, then restore the binding:

```sh
uv run uro provider bind extractor <openai-connection-id> gpt-4o-mini
uv run python scripts/extraction_replay.py <war-story world id>
uv run python scripts/extraction_replay.py <Leg 1 world id>
uv run uro provider bind extractor <previous connection id> <previous model>
```

Read back: the party's two PCs and whose beat each was (`uro events`), the module claim the rule
committed and whether the narrator voiced it, Brinehold's `CheckResolved` outcomes and sheet
changes (strain, wounds), and each replay's claims beside the original.
