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
POST /worlds                     {name, tone?, rule_pack?}         create world (JSON body; OPERATOR-only, D-46)
POST /worlds/validate            (multipart .zip of the pack)      grade an uploaded pack, no import (BE-6)
POST /worlds/backfill            (multipart .zip of the pack)      AI gap-fill preview, ai_backfill seeds (OPERATOR-only, D-44) (BE-7)
POST /worlds/probe               (multipart .zip) [?tries=]        model-capability report, warn-not-fail (OPERATOR-only, D-44) (BE-7)
GET  /worlds                                                       list
GET  /worlds/{w}/branches                                          branch tree + markers, per-branch in-fiction day (BE-1)
GET  /worlds/{w}/log             [?branch=&limit=]                 commit lineage, git-log style (BE-3)
GET  /worlds/{w}/events          [?branch=&type=&entity_ref=&caused_by=&limit=]  raw event log (OPERATOR-only, D-45) (BE-4)
GET  /worlds/{w}/commits/{id}                                      one commit's events (OPERATOR-only, D-45) (BE-4)
POST /worlds/{w}/branches        {from_ref, name, time_skip_days?}  fork (OPERATOR-only, D-44) (BE-2)
POST /worlds/{w}/markers         {name, branch?}                   name a branch head (OPERATOR-only, D-44) (BE-3)
GET  /worlds/{w}/export                                            whole world → hash-chained bundle (OPERATOR-only, D-45) (BE-8)
POST /worlds/import              {…WorldBundle JSON…}              verify chain + instantiate a fresh world (OPERATOR-only, D-44) (BE-8)
GET  /worlds/{w}/chronicle       [?branch=&limit=]                a named branch's recent beats (OPERATOR-only) (BE-10)
POST /worlds/{w}/campaigns       {participant, new_pc_name|adopt_actor_id}   start_campaign
GET  /campaigns                  [?world_id=]                      list
GET  /campaigns/{c}                                                one campaign
POST /campaigns/{c}/join         {participant, new_pc_name|adopt_actor_id}   bind an additional PC
GET  /campaigns/{c}/roster                                         active PC ids
GET  /campaigns/{c}/state        [?sections=actors,threads,…]      branch projections; player allowlist, omniscient sections operator-only (D-46) (B5)
GET  /campaigns/{c}/chronicle    [?limit=]                         recent beats
POST /campaigns/{c}/time-skip    {days}                            engine agenda_tick (OPERATOR-only, D-46) (D-33)
POST /campaigns/{c}/dry-run      {intent}                          preview a beat's events, commit nothing (BE-5)
GET  /campaigns/{c}/consistency                                    narrator contradiction-survival proxy T2 (BE-5)
POST /campaigns/{c}/end          {marker, outcome?}                end campaign, release PCs (OPERATOR-only, D-44) (BE-9)
GET  /campaigns/{c}/codex         [?participant=]                  a participant's fork-surviving notes (self/admin, D-39) (BE-9)
POST /campaigns/{c}/codex        {text, participant?, key?, pinned?, refs?}   add a note (self/admin, D-39) (BE-9)
GET  /usage                      [?stage=]                        LLM-call telemetry by stage (OPERATOR-only, D-44) (BE-10)
GET  /rulesets                                                    registry: id@version + sheet shape (any-authed) (BE-10)
GET  /providers                                                   model-connection registry snapshot: connections+roles+credential-metadata (OPERATOR-only, D-47)
POST /providers                  {name, provider, base_url?, auth_id?}   register a connection (OPERATOR-only, D-47)
PATCH  /providers/{id}           {is_enabled}                     enable/disable a connection (OPERATOR-only)
DELETE /providers/{id}                                            delete a connection (bindings cascade) (OPERATOR-only)
POST /providers/credentials      {provider, access_token, auth_mode?}    store a credential, encrypted at rest (OPERATOR-only; 501 w/o URO_SECRET_KEY)
DELETE /providers/credentials/{id}                                delete a credential (linked connections unlinked) (OPERATOR-only)
PUT  /providers/roles/{role}     {connection_id, model}           bind an engine role → connection+model (OPERATOR-only, D-47); embedder needs an embedding model
DELETE /providers/roles/{role}                                    unbind a role (OPERATOR-only)
POST /providers/{id}/refresh                                      discover a connection's models → cached_models w/ modality (LIVE call; OPERATOR-only, D-47)
POST /providers/{id}/test        {model?}                         probe a connection with a 1-token call → {ok, detail} (OPERATOR-only)
POST /providers/reload                                            rebuild the instance router from the registry, no restart (OPERATOR-only)
```

**Model-connection registry (D-47, `docs/20`).** The `/providers` surface configures the
instance-level LLM provider registry over HTTP (so `uro-loom` / any client drives it; `uro provider …`
edits the DB directly). All of it is **operator-only** (D-44 — provider config is a cost/structural
concern). A credential's `access_token` arrives as **plaintext over the (operator-only, TLS-in-prod)
wire** and is **encrypted at rest** under `URO_SECRET_KEY`; **no read ever returns a secret**
(`GET /providers` lists credential *metadata* — `has_access_token`, never the value). At `uro serve`
startup the router is built from the registry when it has bindings, else the `uro.toml`/`--provider`
seed. Per-end-user credentials / org isolation stay a platform/BFF concern (out of scope here).

The shipped `state`/`chronicle` reads are **campaign-scoped** (they resolve the campaign's branch)
rather than the aspirational `world`-scoped `?branch=&at=` form. A REST-created campaign **pins the
world's declared ruleset and sheets its PC** exactly like the CLI `uro campaign new`/`join` path
(D-30 + Phase-3) — so a PbtA world's campaign started over REST plays as PbtA, and the WS
cross-ruleset guard still fires. Bad **input** is `400` (a malformed body, an unknown `?sections=`,
a non-int `?limit=`, `days<=0`), an unknown campaign/world `404`. **World export/import ship over
HTTP (BE-8):** `GET …/export` returns the whole world as a portable, SHA-256 hash-chained
`WorldBundle` JSON (the `.uwp` content); `POST /worlds/import` recomputes that chain and rejects a
tampered bundle with `400` (`ExportError`) **before** any write, else re-instantiates a fresh world
(remapped ids, projections rebuilt by replay). Both are **operator-only** (import a structural write
→ D-44; export bulk omniscient disclosure → D-45). **Telemetry + registry + world-scoped chronicle
ship (BE-10):** `GET /usage[?stage=]` aggregates the `llm_calls` metering by engine stage (count,
token sums, avg latency) — **operator-only** (D-44, it reveals model/token/latency cost); the engine
only *exposes* metering, it never bills (docs/00). `?world=`/`?campaign=` are **not supported yet**
(the `llm_calls` rows carry no world/campaign column) and return `400` rather than silently ignoring
the filter — a consumer never mistakes a global total for a per-world one; keying metering by
world/campaign is a forward-only migration + threading campaign context through the `_meter` seam
(deferred). `GET /rulesets` lists each built-in ruleset's `id`, `version`, and sheet schema
(any-authed — public capability info). `GET /worlds/{w}/chronicle[?branch=&limit=]` is the
**world-scoped** twin of the campaign chronicle: it reads ANY named branch's recent beats (incl.
sibling what-if forks), so it's a GM/operator timeline-inspection surface (operator-only, same family
as `/log`, `/events`). The world-scoped **`state?branch=&at=`** (materialize projections at an
arbitrary commit — nearest-snapshot + replay) is **deferred to its own slice**: it's a new read-only
materialize-at-commit core primitive (not the head-only `query_across` the campaign `/state` uses),
which deserves a focused implementation + snapshot-correctness tests, not a bolt-on. **The AI
world-authoring stages ship (BE-7):** `POST /worlds/backfill` previews a thin pack's AI gap-fill
(the augmented seeds, each tagged `provenance=ai_backfill`, + before/after grade) and `POST
/worlds/probe[?tries=]` returns the judge-scored model-capability report. Both are
**operator-only** (D-44 — they make live, uncapped LLM calls; the engine exposes cost but never
caps it) and **pack-UPLOAD-shaped** (multipart `.zip`, like `/worlds/validate`), NOT `/worlds/{w}/`:
backfill's sufficiency gaps read `manifest.generate_population`/`history`/lore and probe reads
`manifest.content`, none of which a stored world persists — so the pack must be re-supplied.
Backfill is **preview-only** (mirrors `uro world backfill` — it commits nothing); committing the
`ai_backfill` `ThreadCreated` seeds is `world create --backfill`, which rides the deferred
pack-upload **create** endpoint. Probe is **warn-not-fail** (D-24): a weak/refusing model yields
`status=warn|fail` in a `200` report (`ok` is the machine verdict), never an HTTP error; a real
provider transport failure is a `502`, a malformed pack a `400`, and an unwired provider a `501`.
An over-cap upload (Content-Length > 20 MB) is `413`'d by a small middleware **before** the body is
spooled — so a large body can't be buffered pre-auth on these operator-only routes (the same guard
now also fronts `/worlds/validate`; `/worlds/import`'s legitimately-large JSON bundle is exempt).
CI never makes live calls — the stub provider drives the deterministic path; the live pass is the
operator's. Still **CLI-only / scaffolded** (deferred, not regressed): world `seed`, the SSE `POST
…/beats` (play is WS), and world `state?at=`. `seed` is
carved out on purpose — `seed_history` needs the pack's `manifest.history` (era, simulate_years),
which the world does **not** persist in a `seed_history`-usable form (`WorldGenesis` stores only
name/tone/overrides/ruleset/rule-pack), so a `{seed}`-only body can't reconstruct it; seeding needs
the pack re-supplied and so belongs with the deferred pack-upload **create** endpoint, not a bare
seed call. **Authority:** the timeline summary reads (`/branches`, `/log`) are plain any-authed reads; the
*structural writes* — `POST …/branches` (fork) and `POST …/markers` — are **operator-only** (D-44;
require an `--admin-token`, a plain player token → 403), via `_require_operator` on the same
`is_admin` choke point as D-39's self-or-admin scoping. The **raw event log** (`GET …/events`,
`GET …/commits/{id}`) is ALSO operator-only, but for a different reason (**D-45**): the raw log
carries omniscient truth — `ClaimRecorded` truth-values, hidden beliefs, `caused_by` — that the
non-omniscient player reads deliberately never expose, so it is a GM/operator observability surface.
The **dry-run** (`POST …/dry-run`) is any-authed — the non-committing twin of a play beat — and is
**intent-only** (no client `plan=`, D-37: a network-supplied plan would first have to route through
the D-32 protection ceiling, so this path never accepts one). Otherwise authority is coarse — a
valid token authorizes the call and the acting `participant` is taken from the body (finer
endpoint→campaign authority is deferred, docs/18 P3).

**Holistic-review hardening (D-46).** A system-wide cross-phase review of the whole BE-1…BE-11
surface tightened three seams. **Authority:** `POST /worlds` (create) and `POST
/campaigns/{c}/time-skip` are now **operator-only** — both are the same structural timeline write as
their already-operator siblings (`import` / fork time-skip / `end`), which D-44's *principle*
covers even though its enumerated list omitted them; time-skip also caps `days`. **Epistemic
(D-45 enforced, not just default):** `GET /campaigns/{c}/state` restricts a **player** token to the
scene-safe sections `{actors,threads,places,factions,pcs}` and `403`s any omniscient section
(`claims`' truth values, `beliefs`, `sheets`, `items`, `edges`, `counters`) — an **operator** reads
anything. **Scope + hygiene:** `start_campaign` gains the self-or-admin check (a player may name only
themselves, like `join`); `time-skip`/`dry-run` gain the minted-token campaign-scope check the
WS/outcome paths already enforced; the mechanics `seed` is stripped from a player-facing campaign
read; `beat_failed` broadcasts a generic reason (the raw exception is logged, not fanned out); pack
upload caps decompressed size (zip-bomb); `?limit<0` and a non-int `seed` are `400`; `/roster` `404`s
an unknown campaign.

### The WebSocket wire contract (BE-11 — frame-for-frame with `app.py`)

Every frame is a JSON object with a `type`. There is **no** universal envelope: a frame carries only its `type` + the fields listed below. The channel is already scoped to one campaign by the connection URL, so frames do **not** repeat `campaign_id`; `participant_id` names the actor of the frame (absent only where there is none, e.g. `vote_decided`).

**client→server** (any other `type` is silently ignored):

| frame | fields | effect |
|---|---|---|
| `intent` | `text` | run a beat as this participant's PC (canonical — commits) |
| `table_talk` | `text` | the non-canon coordination lane (D-38) — broadcast only, never a beat |
| `vote` | `choice` | cast a vote on a `--arbiter vote` server (D-38) |

**server→client** (all broadcast to every connection on the campaign):

| frame | fields |
|---|---|
| `participant_joined` / `participant_left` | `participant_id` |
| `beat_started` | `participant_id`, `intent` |
| `narration_chunk` | `participant_id`, `text` |
| `beat_committed` | `participant_id`, `intent`, `narration` |
| `beat_failed` | `participant_id`, `intent`, `error` |
| `not_your_turn` | `participant_id`, `text` *(round-robin, D-31)* |
| `proposal_opened` | `participant_id`, `text` *(a QUEUED non-holder intent, D-38)* |
| `intent_rejected` | `participant_id`, `text` |
| `table_talk` | `participant_id`, `text` |
| `vote_tally` | `participant_id`, `choice`, `tally` *(D-38)* |
| `vote_decided` | `choice` |
| `vote_unsupported` | `participant_id` *(the server's arbiter has no vote shape)* |
| `outcome_recorded` | `encounter_id`, + the distilled receipt (`committed_events`, `commit_id`, …) *(Chronicler mode, D-25 — broadcast when an external outcome is posted)* |

**Deliberately NOT emitted yet (a future GROW, not drift).** `scene_update` / `mode_change` (the highest-value add — Loom's Play surface can't show scene/mode) need the pipeline to surface within-beat mode state (encounters auto-resolve inside one beat under D-29, so between beats the mode is always free-roam — there is no persistent mode frame to send without new engine state). `mechanics_result` needs the same. `suggestions` (the planner's D-23 next-action hints) and a `beat_committed` `beat_id` (for deep-linking a committed beat) both need the `run_beat` streaming contract to yield structured frames, not only narration strings. `encounter_action` waits on interactive per-turn play (D-29); `pin_actor` on PC-anchored recall. Each is a deliberate transport extension, tracked for when its engine state exists.

Beat results may carry `suggestions[]` — 2–4 affordance-grounded next-action hints emitted by the planner at no extra LLM cost (D-23). **Free-text intent is canonical**; suggestions are hints clients may ignore entirely (the CLI renders them dimmed), never a constrained choice list. (Over the WS wire they are a future GROW, above; the embedded/CLI path already surfaces them.)

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
uro serve --token dev --cors-origin http://localhost:5173   # allow a browser SPA (uro-loom) — see "Browser access" below
uro usage <campaign>
```

`uro play` and `uro dry-run` are the two commands the whole roadmap's acceptance tests run through.

**Browser access (CORS).** A browser SPA (the `uro-loom` console) is a *different origin* from the
server, so without a CORS header the browser blocks every cross-origin call — the request never
reaches the app (a `curl` from the shell still works, which is the tell). The server sends **no CORS
header by default** (CLI/embedded use needs none, and a permissive default would be an unsafe
surprise); a deployment opts in with `uro serve --cors-origin <origin>` (repeatable — list each
allowed origin; `*` allows any origin for pure dev and then drops credentials per the CORS spec).
For a local Loom dev loop that's `--cors-origin http://localhost:5173`. A production deployment can
instead (or additionally) terminate CORS at a reverse proxy, or route the browser through the
optional M6 BFF (Loom's [`docs/05-bff-design.md`](https://github.com/cupskeee/uro-loom)) so calls are
same-origin.
