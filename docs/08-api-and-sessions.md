# 08 — API and Sessions

`uro-server` is a thin FastAPI shell over `uro-core`: transport, sessions, auth, wiring — no engine logic. Everything here is also callable in-process by embedding `uro-core` directly (the CLI does both: embedded for local play, HTTP client mode against a server).

## Surface

REST for management, WebSocket for play. All responses JSON; narration streams.

```
# Worlds & timeline
POST   /worlds                          create from world pack (multipart or path)
POST   /worlds/{w}/seed                 run History seeding (idempotent per seed)
GET    /worlds/{w}/branches             list branches, heads, markers
POST   /worlds/{w}/branches             fork: {from_commit | from_marker, name}
GET    /worlds/{w}/chronicle?branch=B   the Lore Wall projection (paginated)
GET    /worlds/{w}/state?branch=B&at=C  materialized state queries (entity, edges)
POST   /worlds/{w}/probe                run capability probes → report
GET    /worlds/{w}/export | POST /worlds/import

# Campaigns & play
POST   /worlds/{w}/campaigns            {branch | fork_spec, party_spec, seed}
GET    /campaigns/{c}/scene             current scene projection
POST   /campaigns/{c}/beats             submit intent (SSE stream) [?dry_run=true]
POST   /campaigns/{c}/encounters/{e}/outcome   external resolver reports an outcome bundle (Chronicler mode, D-25)
WS     /campaigns/{c}/play              interactive channel (below)

# Ops
GET    /usage?world=&campaign=&stage=   token/latency metering
GET    /healthz
```

### What actually ships (docs/18 B3)

The table above is the aspirational surface. Until B3 only `WS /campaigns/{c}/play` and the
Chronicler `POST …/outcome` were wired — every other management op forced embedding `uro-core`
(the "*Uro has a server* vs *Uro is a server*" gap four games hit). B3 lands a real, authed
management surface over the `EngineStore` port (`ServerDeps.store`; `501` when a deployment wires
transport-only deps). **Built now:**

```
POST /worlds                     {name, tone?, rule_pack?}         create (JSON body, not pack-upload yet)
GET  /worlds                                                       list
POST /worlds/{w}/campaigns       {participant, new_pc_name|adopt_actor_id}   start_campaign
GET  /campaigns                  [?world_id=]                      list
GET  /campaigns/{c}                                                one campaign
POST /campaigns/{c}/join         {participant, new_pc_name|adopt_actor_id}   bind an additional PC
GET  /campaigns/{c}/roster                                         active PC ids
GET  /campaigns/{c}/state        [?sections=actors,threads,…]      query_across the branch's projections (B5)
GET  /campaigns/{c}/chronicle    [?limit=]                         recent beats
POST /campaigns/{c}/time-skip    {days}                            engine agenda_tick (D-33)
```

The shipped `state`/`chronicle` reads are **campaign-scoped** (they resolve the campaign's branch)
rather than the aspirational `world`-scoped `?branch=&at=` form; a malformed body is `400`, an
unknown campaign/world `404`. Still **CLI-only / scaffolded** (deferred, not regressed): world
`seed`/`branches`/`probe`/`export`/`import`, the SSE `POST …/beats` (play is WS), and `GET /usage`.
Authority is coarse — a valid token authorizes the call, but the acting `participant` is taken from
the body (finer endpoint→campaign authority is deferred, docs/18 P3).

The WebSocket channel carries: client→server `intent`, `encounter_action` *(future — encounters auto-resolve in the PoC, D-29)*, `pin_actor`; server→client `narration_chunk`, `scene_update`, `mechanics_result`, `mode_change`, `beat_started`, `beat_committed`, `beat_failed`, `not_your_turn` *(round-robin turn arbitration, D-31)*, `intent_rejected`, `suggestions`, `participant_*`. Message envelope always includes `campaign_id`, `beat_id`, and `participant_id` — that last one is the multiplayer seam (below).

Beat results may carry `suggestions[]` — 2–4 affordance-grounded next-action hints emitted by the planner at no extra LLM cost (D-23). **Free-text intent is canonical**; suggestions are hints clients may ignore entirely (the CLI renders them dimmed), never a constrained choice list.

## Session model — multiplayer (round-robin, D-31)

Owner requirement: single-player MVP, **architecturally prepared** for multiplayer. Phase 7 (OQ-7 → D-31) made the preparation real — a single-player leak audit (35 findings) proved the "shaped-for-MP" seam had leaked single-player assumptions (the pipeline planned every beat as one campaign-wide PC; `admit` was a bare bool; `Session`/`Participant` were dead-wired), and building a real `PartyArbiter` forced them out.

- **Campaign ≠ Session.** A *campaign* is the persistent story (branch, party, mode). A *session* is a live connection context. A campaign's PARTY is now real: `start_campaign` seats the first participant's PC; `bind_pc` / `uro campaign join` seats each additional participant on their OWN PC (one `PCBound` per participant). ~~Nothing anywhere assumes party size 1~~ — corrected: it *did* (`campaign_pc` was singular); now the pipeline resolves the acting participant's PC via `pc_for_participant`, with `campaign_pc` kept only as the solo fallback.
- **Participants, not "the player."** Every intent, beat, and event `caused_by` carries a `participant_id` mapped to a PC actor — and a beat is now PLANNED and RESOLVED as the submitting participant's PC (the planner "YOU ARE actor X", the mechanics gate, the encounter aggressor). Single-player is the degenerate one-participant case.
- **Arbitration port.** Beat admission goes through the `TurnArbiter` port: `admit -> AdmitDecision` (`ADMITTED` / `NOT_YOUR_TURN` / `REJECTED`; `QUEUED` reserved) + a `note_joined`/`note_left`/`beat_committed` lifecycle. `SoloArbiter` always admits; `PartyArbiter` (D-31) rotates one turn token per campaign over the connected roster in join order (only the holder is admitted; the token advances on `beat_committed`). Turn state is **session-only** (not event-sourced — a turn token is a live-connection concern, not campaign history). Proposal-window / consensus / GM-player arbiters remain future implementations behind the same port (OQ-7's genuinely-open part); encounter mode still self-arbitrates via initiative.
- **Broadcast-shaped output.** Server→client messages are addressed to a session and fan out to all its connections; with one connection this is invisible, with four it's already correct.
- **What is deliberately NOT built (deferred):** proposal-window/consensus arbiters; auto-binding a PC on WS-join (`uro campaign join` pre-seats the party); an optimistic-concurrency `expected_head` guard (round-robin serializes turns, so concurrent free-for-all beats aren't reachable); lobby discovery, invitations, per-participant hidden information, voice/chat (platform/post-MVP).

## Auth & identity

The engine has **no user system** (platform concern). Server modes:

- `local` (default): bind localhost, no auth — the solo dev loop.
- `token`: static bearer token(s) from config, per-token participant identity — enough to test "two clients, one campaign" without building accounts.
- OAuth/OIDC integration, org/user management: platform layer, never in the engine.

Content settings do **not** live on any identity — they live on the world manifest (`09-world-definition.md`), per owner feedback.

## CLI reference client (`uro-cli`)

The only first-party consumer; doubles as the dev harness. Command sketch:

```
uro world create ./worlds/ashfall/        uro world seed ashfall --seed 42
uro world probe ashfall                   uro world validate ./worlds/ashfall/
uro campaign new ashfall --branch main --pc wizard.yaml
uro play <campaign>                       # interactive loop (rich TUI-lite: streamed prose + status line)
uro dry-run <campaign> "I kick the door"  # pipeline without commit; prints proposed events
uro branch fork ashfall --at campaign-a-end --name aftermath
uro log <world> [--branch B]              # chronicle view, git-log style (defaults to main; per-branch lineage, never a cross-branch merge — `02`)
uro world export ashfall -o ashfall.uwp   # hash-chain-stamped bundle;  uro world import ashfall.uwp verifies + instantiates
uro serve --token alice --token bob       # run the server;  uro connect <campaign> --server URL --token alice  (WS client)
uro usage <campaign>
```

`uro play` and `uro dry-run` are the two commands the whole roadmap's acceptance tests run through.
