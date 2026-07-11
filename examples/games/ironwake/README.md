# IRONWAKE

**Battle Brothers / Darkest Dungeon, but the world remembers.** You captain the Ironwake
Company, a band of sellswords in the war-bruised Marches: take a contract in a tavern, march to
some muddy field, and fight a grid-based, turn-based, permadeath skirmish that IRONWAKE's own
engine resolves — then come home and listen to the world talk about what you did. Every fight
is reported to the **Uro engine** through its Chronicler contract, and Uro decides what the
world believes: the dead stay dead forever, great deeds propagate as rumors that hedge and
fade with distance (confident in your home tavern, "has heard a rumor" two towns out, silence
beyond), a famous enemy captain **cannot** be recorded dead no matter how cleanly you cut him
down (the bounty is never paid on a story), and a last stand where nobody survives leaves no
story at all. At season's end, one `fork_branch` call replays a different final choice from the
very same chronicle and the two histories visibly diverge.

IRONWAKE is also a **forcing function**: it is the first serious consumer of Uro's Chronicler
posture, built to drive that surface into every place it is thin and report back. The game
never modifies Uro; every refusal, downgrade, or forced workaround is logged at the call site
(`frictionlog.py`) and printed with every run. The scientific output is
[`GAP_REPORT.md`](GAP_REPORT.md) — headline findings: the missing REST read surface makes the
network posture a fiction, the declarative Reaction Layer could express none of the seven
counter-shaped rules the game wanted — six refused outright, and a seventh (a quantified
member-of trigger) **validated yet can never fire**, the sharper footgun — every counter that
fell into game code is also a fork-consistency bug, and the protection ceiling is
simultaneously the game's best scene and a structural wall for the kill-the-boss genre.

## Run it

```sh
docker compose up -d --wait          # Postgres + pgvector on HOST PORT 5433
uv run uro db migrate
cd examples/games
uv run python -m ironwake.cli play --seed 7                      # the full season (embed)
uv run python -m ironwake.cli play --seed 7 --posture server     # same season over HTTP/WS
uv run python -m ironwake.cli seed                               # inc-0: seed + world read-back
uv run python -m ironwake.cli battle --seed 7 --verbose          # inc-1: one headless skirmish
```

**No API key.** Narration uses Uro's deterministic stub provider and all combat dice flow from
one seeded RNG — the same seed replays the identical season (same casualties, same feats, same
ending). The run self-verifies with printed CHECK lines and exits nonzero on any failure — 22
on the documented seed 7; some checks are conditional on that season's events (a downgrade, a
wipe, a routed witness), so other seeds print fewer. A real model is opt-in:
`--provider openai|anthropic [--model ...]` (key in env, never required).

Tests (DB tests skip if Postgres is down):

```sh
uv run pytest examples/games/ironwake/tests     # from the repo root
```

## Postures & Uro surface exercised

- **`--posture embed`** (Posture A, the CI path): `uro_core` in-process — `create_world`
  (authored seed events + inline `rule_pack`), `start_campaign`, `distill_outcome` +
  `append_beat` + `engine.react`, `engine.run_beat` town scenes, `agenda_tick`/`time_skip`,
  `fork_branch`, and the full projection read surface (`list_actors`, `claims_about`,
  `beliefs_of`, `list_threads`, `list_edges`, `items_owned_by`, `get_branch`, ...).
- **`--posture server`** (Posture B): boots `uro serve --provider stub` as a subprocess, POSTs
  each battle's **OutcomeBundle** to `/campaigns/{c}/encounters/{e}/outcome` and narrates
  taverns over the WS `/play` channel — while every read still goes through the embedded
  library, because no HTTP read surface exists. That asymmetry is deliberate evidence; see
  GAP_REPORT.md rows 1–2.
- **Chronicler trust model (D-32), demonstrated not defeated:** tier-0/1 casualties become real
  `ActorDied` canon (mercs permadie); tier-2 Captain Vorlund's death **downgrades** to
  `truth=unknown` testimony and his blade's loot is refused — twice a season; feats propagate
  along authored `knows` distance-chains with per-hop confidence decay that the narrator prompt
  renders as certainty phrasing; a witnessless wipe records deaths but spreads nothing; bundle
  replays are idempotent (the retry probe); one adversarial out-of-scope bundle is probed on a
  throwaway fork.
- **Reaction Layer:** an `ActorDied` war-ratchet (dormant → offered → active), a loot-triggered
  war resolution (`ItemTransferred` of the Red Band standard → `resolved`), and downtime
  agendas (war rumors, the formal war edge) — plus a live, printed refusal of the counter rule
  the game actually wanted, and the 7-entry wished-rule refusal log.

## Files

| file | role |
|---|---|
| `cli/__main__.py`, `cli/season.py` | entry point; the season loop, dramatized beats, probes, fork |
| `game/` | the tactics engine: units, 10×10 grid battles, scenarios, roster (no Uro imports) |
| `world/setup.py` | the Marches: places, factions, tiers, threads, the `knows` distance-chains |
| `world/rules.py` | the declarative rule pack + `WISHED_RULES` (the refusal log) |
| `world/chronicle.py` | battle log → OutcomeBundle → report → canon read-back |
| `world/uro.py`, `world/reads.py` | embed/server backends; the library read layer (both postures) |
| `frictionlog.py` | call-site gap/refusal collectors, printed every run |
| `tests/` | determinism, Chronicler contract, downgrade, silence, near/far decay |
| [`GAP_REPORT.md`](GAP_REPORT.md) | **the scientific output** |
