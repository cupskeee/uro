# THE SEVENTH VAULT

A runnable, deterministic, **multiplayer heist one-shot built ON the Uro engine** — four
scripted thieves share ONE campaign over the real `uro serve` WebSocket channel, crack a
five-layer vault, trip a declarative alarm, survive a Chronicler-reported guard skirmish, and
walk out clean — or one of them walks out alone. Every consequence (the alarm state, the
prize's owner, who died, what rumor spread) is **committed Uro state read back through
projections**, byte-identical on every run, with **zero API keys**.

Like its siblings (`sable-court`, `ironwake`) this game is a **forcing function**: it was built
to push Uro's multiplayer/session/Chronicler seams until they bend, and its real deliverable is
[`GAP_REPORT.md`](GAP_REPORT.md) — including one genuine engine bug found by play
(`PartyArbiter` misrotates the token when the turn-holder disconnects).

## Run it

```sh
./run.sh              # Postgres up + migrate + BOTH endings, each twice, byte-compared
./run.sh --stress     # + the S1-S8 stress battery (evidence under out/stress/)

# individual pieces
uv run python arc.py --ending clean       # the crew escapes together
uv run python arc.py --ending betrayal    # the Ghost walks out alone
uv run python stress/run_all.py           # the battery only
uv run pytest examples/games/seventh-vault/tests   # the golden-state contract (from repo root)
```

Requires the workspace (`uv sync --all-packages`) and Postgres on host port 5433
(`docker compose up -d --wait`). The tests are NOT collected by `just test` (root `testpaths`
is `packages/`) — run them explicitly as above. **Real-model mode** (opt-in, non-deterministic):
`uv run python arc.py --ending clean --provider openai` with `OPENAI_API_KEY` set. **Real-human
play is transport-possible but NOT a turnkey flag** — a known limitation, and itself a finding:
the stock client (`uv run uro connect <campaign_id> --server http://127.0.0.1:<port> --token
crew-ghost`, campaign id from `out/run_manifest-*.json`) can authenticate and take turns on any
seat, but the default arc cannot host one: it seats all four scripted thieves, asserts every
beat byte-for-byte, and tears the server down when the script ends — and, deeper, the whole
heist is wired to EXACT intent strings (the Reaction-Layer alarm rules pattern-match them,
G-2/S7), so a human Brakk who types anything but the scripted blunder line never trips the
alarm. Human seats want the engine gaps fixed (committed check outcomes, non-canon table talk),
not a bigger client.

## What one run does

1. **Host (Posture A, `host.py`)** — embeds `uro_core` to do everything the HTTP surface
   cannot: creates the world (5 layers, the House Guard, Warden Kessler t3, guards t0/1, the
   `t:alarm`/`t:score` threads, `i:prize`, the inline `HEIST_RULE_PACK`), seats 4 crew PCs on
   one campaign (`start_campaign` + `bind_pc`), and writes `out/run_manifest-<tag>.json` —
   a file is the lobby, because no discovery endpoint exists.
2. **Server (Posture B)** — boots real `uro serve --provider stub` with 4 tokens
   (`PartyArbiter` round-robin); four `ScriptedPlayer` WS clients (`client.py`, `players.py`)
   play 20 beats under strict turn discipline; every client receives the identical committed
   narration (the shared scene is asserted byte-for-byte across all 4).
3. **The alarm (Reaction Layer, `rule_pack.py`)** — three scripted blunders escalate
   `calm → suspicious → alerted → lockdown` purely via pack rules firing on committed
   `BeatResolved` events. Two honesty notes: the grammar's closed `ThreadState` vocabulary
   forces a pun (`calm`=dormant, `suspicious`=offered, `alerted`=active, `lockdown`=dead), and
   with no counters or check-outcome events the rules must pattern-match **exact intent
   strings** (the S7 wall, measured in `stress/s7_counters.py`).
4. **The guard response (Chronicler, `heist.py`)** — at lockdown the game rolls its own seeded
   skirmish and POSTs an `OutcomeBundle` to `/outcome`: the tier-0 guard dies as canon, the
   tier-3 Warden's "death" is **downgraded to `truth=unknown` testimony** (D-32 working as
   designed), the surviving witness carries the rumor (confidence 0.9 → 0.495 one `knows` hop
   later), and the betrayal ending's zero-witness scuffle propagates **nothing**.
5. **The endgame** — taking the Heart and the double-cross are host-authored
   `ItemTransferred` events (a free-roam beat cannot commit one — gap), the score thread
   reacts (`pending → prize-taken → escaped|betrayed`), and a downtime `agenda_tick(7)`
   spreads whichever legend the ending earned.

## Uro surface exercised

Posture B (WS play channel, PartyArbiter, `POST /outcome`, `/healthz`) + Posture A (world
building, PC binding, projections, `append_beat`, `engine.react`, `agenda_tick`, forks not
required) + the Reaction Layer (post-beat rules AND downtime agendas, inline pack) + the
Chronicler trust model (protection ceiling, participant scope, ownership checks, idempotent
claim ids, zero-witness silence) + `uro-basic` sheets (adopted PCs with authored ability
spreads; wounds land as opaque `SheetUpdated`).

## The stress battery (`stress/`)

One runnable probe per TASK target, each writing evidence to `out/stress/`:

| probe | target | headline |
|---|---|---|
| `s1_arbiter.py` | arbiter shapes (OQ-7) | simultaneous / proposal / vote / interrupt all refused; the shapes named |
| `s2_vantage.py` | PC-anchored recall | attribution real, vantage absent (`assemble_recall` has no PC arg) |
| `s3_race.py` | turn-token race | race-safe in-process; atomic by event loop, not by lock |
| `s4_management.py` | REST surface | 10/10 lobby calls → 404; network-only client can boot nothing |
| `s5_lifecycle.py` | join/leave/reconnect | **engine bug**: holder drop misrotates the token backward; reconnect reshuffles the ring; late seats can't authenticate |
| `s6_ruleset.py` | one ruleset/process | enforced cleanly; moot here — stub reaches no mechanics anyway |
| `s7_counters.py` | Reaction-Layer counters | the triple wall: no check events, no counters, closed state vocab; rolls invisible + irreproducible across runs |
| `s8_time.py` | game↔world time | beats never advance the clock under ANY provider (`time_cost` is a dead field); the epilogue clock is hand-rolled; a 14-day tick fires a 7-day agenda once |

Evidence files under `out/` (frame captures, digests, the manifest) are **regenerated** by
`./run.sh --stress`, not committed — the gap table's Evidence column cites committed
`file:line` throughout.

## Files

`world.py` (authored world + the state-pun tables) · `rule_pack.py` (`HEIST_RULE_PACK` + the
refusal log) · `host.py` (Posture-A host + manifest + `uro serve` subprocess) · `client.py`
(WS client) · `players.py` (scripts + turn discipline) · `heist.py` (director: skirmish,
bundles, authored canon, digest) · `arc.py` (the full run) · `frictionlog.py` (the
instrument) · `stress/` · `tests/` (golden contract). Uro itself is **unmodified** — all game
code lives in this folder.
