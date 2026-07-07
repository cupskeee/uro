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
