# 21 — Beat latency: where the time goes, what shipped, what's designed and deferred (D-71)

A beat used to keep the player waiting until it was fully saved, and the slowest part of that came
**after** the story had finished on screen: the extractor. This document records the measurements,
the two changes that shipped, and the larger design that was worked out and then deferred.

## Measurements (2026-11-01, live, the owner's registry models)

Stage latency over the previous 30 hours of real calls (`llm_calls`, no errors):

| Stage | Model | Average | Slowest 10% | Tokens in → out |
|---|---|---|---|---|
| planner | `gpt-5.6-luna` | 2.8 s | 4.2 s | 634 → 148 |
| narrator / dialogue | `gpt-5.6-luna` | 3.3 s | 4.7 s | ~800 → ~160 |
| **extractor** | `gpt-5.6-luna` | **5.1 s** | **8.8 s** | 2355 → 382 |
| embedder | `text-embedding-3-large` | 0.9 s | 1.0 s | — |

The same 12 recorded Ashfall narrations replayed through the extractor
(`scripts/extraction_replay.py`), changing only one thing at a time:

| Extractor variant | Average | Slowest 10% | Recorded |
|---|---|---|---|
| reasoning effort: default | 6.6 s | 12.3 s | 19 claims, 3 threads |
| reasoning effort `low` | **4.0 s** | **5.7 s** | 18 claims, 2 threads, the same key news |
| reasoning effort `none` | 2.4 s | 3.4 s | 29 claims: atmosphere as fact, the same news restated |
| `low` + the prompt's examples table removed (−18% instructions) | 4.3 s | 5.9 s | 16 claims |

Reasoning, not reading, is the cost: trimming the instructions saved nothing, `low` cut the
extractor ~40% with no visible loss, and `none` lost quality. So the prompt was left alone.

Live, through `uro serve`, time from sending an intent (three beats each):

| | Story finished (`beat_narrated`) | Beat saved (`beat_committed`) | Left after the story |
|---|---|---|---|
| extractor at the default | 5.1–8.7 s | 12.1–15.2 s | 3.4–8.7 s (avg 6.7) |
| extractor at `low` | 4.7–6.7 s | 9.8–12.7 s | 3.1–6.6 s (avg 4.9) |

"Left after the story" is extraction plus the memory embedding, the reaction pass and the commit.

## Shipped

1. **A per-role reasoning effort** (`role_bindings.reasoning_effort`, migration 026):
   `uro provider bind extractor <conn> <model> --reasoning-effort low`, or `reasoning_effort` on
   `PUT /providers/roles/{role}`. Sent by the Codex adapter as `reasoning.effort` and by the
   OpenAI-compatible adapter as `reasoning_effort`; refused for connection kinds that send none.
   Which levels a model accepts is the provider's call (`gpt-5.6-luna` via Codex: `none`, `low`,
   `medium`, `high`, `xhigh`); check one first with `uro provider test <conn> --model <m>
   --reasoning-effort low` (or `reasoning_effort` on `POST /providers/{id}/test`). On an OpenAI-style
   connection an effort also means the reasoning-model request shape: no `temperature`, which OpenAI
   rejects beside an effort on GPT-5-family models. Recommended: `low` on the extractor.
2. **The prompt returns when the story is told.** `Engine.run_beat_stream(mark_narrated=True)`
   yields `NARRATION_DONE` between the narration and the commit; `uro serve` turns it into a
   `beat_narrated` frame (with the intent's `intent_id`), and `uro connect` frees its prompt on it.
   An intent typed then waits on the server until the beat commits (or fails), so one player's beats
   never overlap and recall stays consistent. An empty narration, which fails the beat, is never
   marked. The player reads and types while the extractor works.

## Designed and deferred: background extraction

The full version commits a beat in two steps:

- **At the end of the narration**, commit `BeatResolved` with the beat's mechanical events
  (checks, sheets, an encounter): everything decided before the narration.
- **Then, in the background**, run the extractor and commit its events as a follow-on commit,
  then the memory embedding and the reaction pass. Extraction runs one beat at a time per branch,
  so two extractions never race on the same pre-state (duplicate actors, double claims).
- **The next beat** may start at once. Its recall still holds the previous narration (the recent
  beats) but may lack that beat's extracted facts until the follow-on commit lands; its own
  extraction waits for the previous one.
- **Turn order** in a party could rotate at the first commit, so the next player needn't wait.
- **Costs:** a beat's facts land in a later commit (a fork taken between the two holds narration
  without facts; the live-commit feed and export see two commits); world rules fire after the
  follow-on commit; an extraction that fails leaves the narration without its facts (as a parse
  failure already does today).

**Why deferred:** with `beat_narrated`, the time left after the story (3–7 s at `low`) passes
while the player reads and types, so for a single player it would buy almost nothing. Its real
benefit is in a party, where the next player's turn begins only when the previous beat is saved.
Build it when that wait shows up in play, or when the extractor has to get slower (a larger model,
longer narration) than a player's reading time.
