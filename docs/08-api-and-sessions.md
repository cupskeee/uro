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

The WebSocket channel carries: client→server `intent`, `encounter_action`, `pin_actor`; server→client `narration_chunk`, `scene_update`, `mechanics_result`, `mode_change`, `beat_committed`, `suggestions`, `participant_*`. Message envelope always includes `campaign_id`, `beat_id`, and `participant_id` — that last one is the multiplayer seam (below).

Beat results may carry `suggestions[]` — 2–4 affordance-grounded next-action hints emitted by the planner at no extra LLM cost (D-23). **Free-text intent is canonical**; suggestions are hints clients may ignore entirely (the CLI renders them dimmed), never a constrained choice list.

## Session model — single-player now, multiplayer-shaped

Owner requirement: heavy emphasis on single-player MVP, **architecturally prepared** for multiplayer lobbies. The preparation is structural, not speculative code:

- **Campaign ≠ Session.** A *campaign* is the persistent story (branch, party, mode). A *session* is a live connection context: `session(campaign_id, participants[])`. MVP: exactly one session per campaign, one participant. Nothing anywhere assumes party size 1.
- **Participants, not "the player."** Every intent, beat, and event `caused_by` carries a `participant_id` mapped to a PC actor. Single-player is the degenerate case of a participant list of one.
- **Arbitration port.** Beat admission goes through a `TurnArbiter` interface. MVP implementation: `SoloArbiter` (always admit). Multiplayer later means writing `PartyArbiter` (free-roam: proposal window/consensus — genuinely open, OQ-7; encounter mode: initiative order already arbitrates) — a new arbiter, not a rewrite.
- **Broadcast-shaped output.** Server→client messages are addressed to a session and fan out to all its connections; with one connection this is invisible, with four it's already correct.
- **What MVP deliberately does NOT build:** lobby discovery, invitations, per-participant hidden information, voice/chat. All platform or post-MVP.

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
uro export world ashfall -o ashfall.uwp
uro usage <campaign>
```

`uro play` and `uro dry-run` are the two commands the whole roadmap's acceptance tests run through.
