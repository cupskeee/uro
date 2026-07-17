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
rather than the aspirational `world`-scoped `?branch=&at=` form. A REST-created campaign **pins the
world's declared ruleset and sheets its PC** exactly like the CLI `uro campaign new`/`join` path
(D-30 + Phase-3) — so a PbtA world's campaign started over REST plays as PbtA, and the WS
cross-ruleset guard still fires. Bad **input** is `400` (a malformed body, an unknown `?sections=`,
a non-int `?limit=`, `days<=0`), an unknown campaign/world `404`. Still **CLI-only / scaffolded**
(deferred, not regressed): world `seed`/`branches`/`probe`/`export`/`import`, the SSE `POST …/beats`
(play is WS), and `GET /usage`. Authority is coarse — a valid token authorizes the call, but the
acting `participant` is taken from the body (finer endpoint→campaign authority is deferred, docs/18 P3).

The WebSocket channel carries: client→server `intent`, `table_talk` *(the non-canon coordination lane, D-38)*, `vote` *(consensus, D-38)*, `encounter_action` *(future — encounters auto-resolve in the PoC, D-29)*, `pin_actor`; server→client `narration_chunk`, `scene_update`, `mechanics_result`, `mode_change`, `beat_started`, `beat_committed`, `beat_failed`, `not_your_turn` *(round-robin turn arbitration, D-31)*, `proposal_opened` *(a QUEUED non-holder intent surfaced as a proposal, D-38)*, `table_talk`, `vote_tally` / `vote_decided` / `vote_unsupported` *(D-38)*, `intent_rejected`, `suggestions`, `participant_*`. Message envelope always includes `campaign_id`, `beat_id`, and `participant_id` — that last one is the multiplayer seam (below).

Beat results may carry `suggestions[]` — 2–4 affordance-grounded next-action hints emitted by the planner at no extra LLM cost (D-23). **Free-text intent is canonical**; suggestions are hints clients may ignore entirely (the CLI renders them dimmed), never a constrained choice list.

## Session model — multiplayer (round-robin, D-31)

Owner requirement: single-player MVP, **architecturally prepared** for multiplayer. Phase 7 (OQ-7 → D-31) made the preparation real — a single-player leak audit (35 findings) proved the "shaped-for-MP" seam had leaked single-player assumptions (the pipeline planned every beat as one campaign-wide PC; `admit` was a bare bool; `Session`/`Participant` were dead-wired), and building a real `PartyArbiter` forced them out.

- **Campaign ≠ Session.** A *campaign* is the persistent story (branch, party, mode). A *session* is a live connection context. A campaign's PARTY is now real: `start_campaign` seats the first participant's PC; `bind_pc` / `uro campaign join` seats each additional participant on their OWN PC (one `PCBound` per participant). ~~Nothing anywhere assumes party size 1~~ — corrected: it *did* (`campaign_pc` was singular); now the pipeline resolves the acting participant's PC via `pc_for_participant`, with `campaign_pc` kept only as the solo fallback.
- **Participants, not "the player."** Every intent, beat, and event `caused_by` carries a `participant_id` mapped to a PC actor — and a beat is now PLANNED and RESOLVED as the submitting participant's PC (the planner "YOU ARE actor X", the mechanics gate, the encounter aggressor). Single-player is the degenerate one-participant case.
- **Arbitration port.** Beat admission goes through the `TurnArbiter` port: `admit -> AdmitDecision` (`ADMITTED` / `NOT_YOUR_TURN` / `REJECTED` / `QUEUED`) + a `note_joined`/`note_left`/`beat_committed` lifecycle. `SoloArbiter` always admits; `PartyArbiter` (D-31) rotates one turn token per campaign over the connected roster in join order (only the holder is admitted; the token advances on `beat_committed`). Turn state is **session-only** (not event-sourced — a turn token is a live-connection concern, not campaign history). **Two more shapes ship (D-38, `uro serve --arbiter proposal|vote`):** `ProposalWindowArbiter` (a non-holder's intent returns `QUEUED` — now live — and is surfaced to the table as a `proposal_opened`, not a silent `NOT_YOUR_TURN`; the holder still acts, the decided action runs as one ordinary beat) and `VoteArbiter` (a session-only tally via the optional `VoteCoordinator` capability, decided by strict plurality). Both round-robin like `PartyArbiter`; both ride the **non-canon coordination lane** (`table_talk` / `vote` broadcast via the hub, never reaching `append_beat`) so no proposal/debate/vote burns a canonical beat. GM-player / simultaneous / reactive-interrupt / consensual-PvP arbiters remain deferred behind the same port (below); encounter mode still self-arbitrates via initiative.
- **Broadcast-shaped output.** Server→client messages are addressed to a session and fan out to all its connections; with one connection this is invisible, with four it's already correct.
- **Durable turn ORDER + runtime tokens (D-39, refines D-31).** Turn *order* is reconnect/restart-stable: the arbiter ring is seeded from `store.pc_seats` — the durable PC-binding order recovered from the `PCBound`/`PCReleased` log — so reconnect re-forms the SAME order regardless of connect race. The turn *cursor* stays session-only (D-31's letter kept: a durable cursor would ride `fork_branch` into what-if branches; a full restart re-forms the order but restarts the round). Auth is no longer frozen at launch: a durable, hashed (`sha256`-at-rest), revocable, **campaign-scoped** `session_tokens` registry (migration 018, off the branch axis) sits behind the single `resolve_participant` choke point, so a player added at runtime mints a live token via the authed `POST /join` (`{actor_id, token}`), `POST /tokens`, or `uro token mint`; `/tokens/revoke` revokes. **Token tiers:** ordinary `--token` peers may act only for THEMSELVES; the `--admin-token` operator subset may seat/mint/revoke for others. `uro serve --arbiter` is decoupled from token count (unset = auto solo/party; set explicitly = that shape always, so a runtime-added player on a 1-token launch still gets turns).
- **What is deliberately NOT built (deferred, D-38 — each verified to fit behind the same port):** **consensual-PvP** (the only shape that would edit the P7 anti-grief invariant + the effect path — reserved until a 2nd game evidences *mechanical*, not narrated, consensual PvP; a trusted in-process consent bypass would drop that invariant from by-construction to by-policy); **simultaneous/composite beats** (the only shape that rewrites one-intent-one-beat — a mixed combat+free-roam composite double-books the fight's canon through one fused extraction; own phase when built); **reactive/interrupt** (admitting out-of-band *during* an in-flight beat is a SECOND concurrent writer — needs the deferred `expected_head` guard, below, or an in-process per-campaign beat-lock, settled first); **`take_pending`** (auto-promoting a winning proposal into an on-behalf-of beat). Also still deferred: auto-binding a PC on WS-join (`uro campaign join` pre-seats the party); an optimistic-concurrency `expected_head` guard (round-robin serializes turns, so concurrent free-for-all beats aren't reachable); lobby discovery, invitations, per-participant hidden information, voice/chat (platform/post-MVP). D-39 residuals (named, low): resume-after-crash (the cursor resets over a now-stable order); a per-message token re-check (revoke blocks a NEW connect, not a live socket); a per-participant token cap + revoke-on-`end_campaign` (durable tokens outlive their binding); cross-process token-cache coherence (single-process PoC).

## Auth & identity

The engine has **no user system** (platform concern). Server modes:

- `local` (default): bind localhost, no auth — the solo dev loop.
- `token`: static bearer token(s) from config, per-token participant identity — enough to test "two clients, one campaign" without building accounts.
- OAuth/OIDC integration, org/user management: platform layer, never in the engine.

Content settings do **not** live on any identity — they live on the world manifest (`09-world-definition.md`), per owner feedback.

## CLI reference client (`uro-cli`)

The only first-party consumer; doubles as the dev harness. Command sketch:

```sh
uro world create ./worlds/ashfall/        uro world seed ashfall --seed 42
uro world probe ashfall                   uro world validate ./worlds/ashfall/
uro campaign new ashfall --branch main --pc wizard.yaml
uro play <campaign>                       # interactive loop (rich TUI-lite: streamed prose + status line)
uro dry-run <campaign> "I kick the door"  # pipeline without commit; prints proposed events
uro branch fork ashfall --at campaign-a-end --name aftermath
uro log <world> [--branch B]              # chronicle view — commit lineage (defaults to main; per-branch, never a cross-branch merge — `02`)
uro world export ashfall -o ashfall.uwp   # hash-chain-stamped bundle;  uro world import ashfall.uwp verifies + instantiates
uro serve --token alice --token bob       # run the server;  uro connect <campaign> --server URL --token alice  (WS client)
uro usage <campaign>
```

`uro play` and `uro dry-run` are the two commands the whole roadmap's acceptance tests run through.
