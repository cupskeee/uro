# Changelog

All notable changes to Uro Engine are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: `0.MINOR.PATCH`, where a MINOR bump is a
notable or breaking change and a PATCH is a fix; see [docs/14](docs/14-development-guide.md)).
The authoritative design history lives in [`docs/decisions.md`](docs/decisions.md); the honest
capability map is [`docs/16-honesty-ledger.md`](docs/16-honesty-ledger.md).

## [Unreleased]

### Added
- **`.env` support.** The `uro` CLI auto-loads a `.env` from the working directory at startup
  (via `python-dotenv`, `usecwd=True`), so `URO_SECRET_KEY` / provider keys / `URO_DATABASE_URL`
  need only live in `.env` rather than be exported by hand. An already-exported env var still wins
  (dotenv never overrides). A committed `.env.example` documents the vars (incl. the Fernet-key
  one-liner for `URO_SECRET_KEY`).

## [0.3.0] - 2026-07-21

### Fixed
- **D-47 pre-release holistic-review hardening (2 HIGH + 3 more).** A system-wide adversarial review
  of the whole model-connection registry (slices 1–4 + Loom) found: **(HIGH) a plaintext key could
  leak** — a stored credential with an embedded CR/LF makes httpx raise `Illegal header value
  b'Bearer sk-…'`, whose text was interpolated into the `refresh` 502 detail and the `test` 200 body
  (rendered in the browser); now credentials are sanitized at ingestion (surrounding whitespace
  stripped, an embedded CR/LF → 400) and the refresh/test error surfaces report only the exception
  *type*, never raw provider text. **(HIGH) a defaultless router crashed every beat** — binding a
  role but not `default` built a router with `default=None`, so any unbound engine role hit a
  `KeyError` mid-beat; now `build_router_from_registry` refuses it loudly (`uro serve` won't start,
  `reload` returns `reloaded:false` with an actionable message, dry-run maps the residual to a 400).
  Also: `PUT /providers/roles` rejects an empty model (400); the `uro provider add` hint now steers
  to `default`; the reload-race docstring is corrected (the per-beat router snapshot is a named,
  low-impact deferral). +6 tests; live-verified.

### Added
- **Model-connection registry — slice 1 (D-47, docs/20).** LLM provider config can now live in the
  DB as an instance-level registry, resolved into the provider router at `uro serve` startup: three
  operational tables (migration 020, off the event/branch axis) — `model_connections`,
  `provider_credentials` (API keys **encrypted at rest** with Fernet under an env KEK `URO_SECRET_KEY`
  that must live outside the DB), and `role_bindings` (role → connection+model; the `default` role is
  the router fallback). New `uro provider add | list | rm | bind | unbind` CLI. `uro serve` resolves
  the router from the registry when it has bindings, else falls back to the existing
  `uro.toml`/`--provider`/stub seed (so nothing existing changes). Adds `cryptography` to the
  `uro-core[postgres]` extra.
- **Model-connection registry — slice 2 (D-47, docs/20): the `/providers` HTTP surface.** The
  registry is now configurable over the API (so uro-loom / any client drives it), **operator-only**
  (D-44): `GET /providers` (snapshot — connections + roles + credential *metadata*), `POST`/`PATCH`/
  `DELETE /providers[/{id}]`, `POST`/`DELETE /providers/credentials[/{id}]`, `PUT`/`DELETE
  /providers/roles/{role}`. A credential's key arrives as plaintext over the operator-only wire and
  is encrypted at rest; **no read returns a secret**. `EngineStore` gains the `ModelRegistry` port;
  the role vocabulary is a shared `ROLES` constant.
- **Model-connection registry — slices 3 + 4 (D-47): discovery, validation, reload.** `POST
  /providers/{id}/refresh` lists a connection's models (a live provider call) and caches them with
  their **modality**; binding the **`embedder`** role now **validates** the model is an embedding
  model (rejects a chat model, allows an unclassifiable one). `POST /providers/{id}/test` probes a
  connection with a 1-token call → `{ok, detail}`. `POST /providers/reload` rebuilds the instance
  provider router **without a restart** (new `Engine.rebind_router`). All operator-only. The
  provider-building + `build_router_from_registry` + discovery logic moved to
  `uro_core.providers.registry` (adapter layer) so the server and CLI share it.
- **Timeline + inspection HTTP surface (BE-1…BE-5, #33–#37)** — the branch-graph, event-log, and
  dry-run/consistency endpoints, epic #44:
  - `GET /worlds/{w}/branches` (BE-1) — branch tree + markers + each branch's in-fiction day.
  - `GET /worlds/{w}/log[?branch=&limit=]` (BE-3) — commit lineage, git-log style.
  - `POST /worlds/{w}/branches {from_ref, name, time_skip_days?}` (BE-2) — fork from a commit/marker,
    with optional downtime-agenda time-skip (parity with `uro branch fork`).
  - `POST /worlds/{w}/markers {name, branch?}` (BE-3) — name a branch head.
  - `GET /worlds/{w}/events[?branch=&type=&entity_ref=&caused_by=&limit=]` (BE-4) — the raw event
    log along a branch, filterable; and `GET /worlds/{w}/commits/{id}` (BE-4) — one commit's events.
  - `POST /campaigns/{c}/dry-run {intent}` (BE-5) — preview the events a beat would commit, writing
    nothing (mirrors `uro dry-run`); any-authed, **intent-only** (no client `plan=`, D-37). And
    `GET /campaigns/{c}/consistency` (BE-5) — the narrator contradiction-survival proxy (T2).
  - **Authority:** the summary reads (`/branches`, `/log`) are any-authed; the structural writes
    (fork, marker-create) are **operator-only** (D-44) via a new `_require_operator` gate on the
    existing `is_admin` choke point; and the **raw event log** (`/events`, `/commits/{id}`) is
    **operator-only** (D-45 — it carries omniscient truth: `ClaimRecorded` truth-values, hidden
    beliefs, `caused_by`; never a player read). A new `advance_branch_time` server dep runs the
    fork's `--time-skip-days` via `engine.agenda_tick`.

- **Browser access for the web console — `uro serve --cors-origin` (fix).** The server shipped with
  no CORS headers, so a browser SPA (uro-loom) on a different origin (e.g. `http://localhost:5173`)
  had every cross-origin call blocked by the browser — the console reported "Unreachable" / an
  all-red network log against a perfectly healthy server (`curl` worked; the browser didn't).
  `create_app` now attaches FastAPI `CORSMiddleware` when one or more `--cors-origin` values are
  passed (repeatable; `*` = dev allow-any, which drops `allow-credentials` per the CORS spec). Off
  by default — CLI/embedded use needs none. This settles the "how does a browser reach the server"
  decision docs/08 had deferred to first real-instance contact.

- **Pack validate over HTTP (BE-6, #38)** — `POST /worlds/validate` accepts a **multipart `.zip`**
  of a world-pack directory, extracts it zip-slip-safely to a temp dir, and returns the sufficiency
  grade + per-dimension detail + gaps + ruleset check (mirrors `uro world validate`). Parse-only —
  nothing is imported, no world state is touched — so it's any-authed, guarded by a 20 MB size cap
  and a path-traversal check. Adds `python-multipart` to `uro-server` (FastAPI file parsing). The
  pack-upload *create* (a structural write, operator-only per D-44) is a follow-up.

- **Campaign end + codex over HTTP (BE-9, #41)** — `POST /campaigns/{c}/end {marker, outcome?}`
  ends a campaign (releases its PCs, marks + snapshots the closing commit as a fork root; mirrors
  `uro campaign end`) — **operator-only** (D-44, a timeline lifecycle write). And the **codex**
  (participant memory, D-36): `GET /campaigns/{c}/codex[?participant=]` + `POST …/codex {text,
  participant?, key?, pinned?, refs?}` — out-of-world player notes that survive a fork, **self-or-
  admin** scoped (D-39: a caller reads/writes their own; an operator may act for another). Closes
  the gap the uro-loom console's M3 explicitly deferred (campaign end had no endpoint).
- **World export/import over HTTP (BE-8, #40)** — `GET /worlds/{w}/export` returns the whole world
  as a portable, SHA-256 hash-chained `WorldBundle` JSON (the `.uwp` content; mirrors `uro world
  export`); `POST /worlds/import` recomputes that chain and rejects a tampered bundle with `400`
  (`ExportError`) **before** any write, else re-instantiates a fresh world with remapped ids and
  projections rebuilt by replay (mirrors `uro world import`). Both **operator-only**: export is bulk
  omniscient disclosure (D-45), import a structural write (D-44). `export_world`/`import_world` were
  promoted onto the `EventStore` port (they lived only on the concrete store). Seeding is **not** in
  this slice — `seed_history` needs the pack's `manifest.history`, which the world doesn't persist,
  so `POST /worlds/{w}/seed` belongs with the pack-upload create endpoint, not a `{seed}`-only body.
- **Usage telemetry + ruleset registry + world-scoped chronicle over HTTP (BE-10, #42)** —
  `GET /usage[?stage=]` aggregates the `llm_calls` metering by engine stage (call count, token sums,
  avg latency) — **operator-only** (D-44: it reveals model/token/latency cost; the engine *exposes*
  metering, never bills). `?world=`/`?campaign=` return `400`, not a silent no-op — the metering rows
  aren't keyed by world/campaign yet (a forward-only migration + `_meter` threading; deferred).
  `GET /rulesets` lists each built-in ruleset's `id`, `version`, and sheet schema (any-authed —
  public capability info; wired via a new `ServerDeps.list_rulesets` composition closure so request
  handlers never import concrete rulesets). `GET /worlds/{w}/chronicle[?branch=&limit=]` is the
  world-scoped twin of the campaign chronicle — any named branch's recent beats — **operator-only**
  (it reads sibling what-if forks; same timeline-inspection family as `/log`/`/events`). Adds
  `usage_by_stage` to the `EventStore` port. The world-scoped **`state?branch=&at=`**
  (materialize-at-commit) is deferred to its own slice — it's a new read-only materialize primitive
  (nearest-snapshot + replay), not the head-only `query_across` the campaign `/state` uses.
- **AI world-authoring stages (backfill + probe) over HTTP (BE-7, #39)** — `POST /worlds/backfill`
  previews a thin pack's AI gap-fill (the augmented seeds, each tagged `provenance=ai_backfill`, +
  before/after sufficiency grade; mirrors `uro world backfill` — commits nothing) and `POST
  /worlds/probe[?tries=]` returns the judge-scored model-capability report (structured-output gate +
  content-rating). Both are **operator-only** (D-44 — they make live, uncapped LLM calls; the engine
  exposes cost, never caps it) and **pack-upload-shaped** (multipart `.zip`, like `/worlds/validate`)
  rather than `/worlds/{w}/`: backfill's gaps read `manifest.generate_population`/`history`/lore and
  probe reads `manifest.content`, none of which a stored world persists — so the pack is re-supplied.
  Probe is **warn-not-fail** (D-24): a weak/refusing model yields `status=warn|fail` in a `200`
  report (`ok` is the machine verdict), never an HTTP error; a provider transport failure → `502`, a
  malformed pack → `400`, an unwired provider → `501`. `engine_deps` gains an optional `router`
  (threaded from `serve`, which already builds it) and wires the `ServerDeps.backfill`/`probe`
  closures over the same process provider the Engine uses. CI never makes live calls — the stub
  provider drives the deterministic path; the live pass is the operator's. Committing the backfilled
  seeds (`ai_backfill` `ThreadCreated` at genesis via `create --backfill`) rides the deferred
  pack-upload create endpoint.

### Fixed
- **BE-1…BE-11 holistic-review hardening (D-46)** — a system-wide cross-phase seam hunt over the
  merged management surface (the project's phase-end discipline, which the BE epic had not yet had)
  found and fixed:
  - **Epistemic leak (HIGH, D-45 now enforced):** `GET /campaigns/{c}/state?sections=claims,beliefs`
    let a **player** token read claim `truth` values + every NPC's hidden beliefs (also
    sheets/items/edges/counters) — the omniscient ground truth the engine exists to hide. The read is
    now restricted to a scene-safe allowlist `{actors,threads,places,factions,pcs}` for a player;
    omniscient sections require an operator token. D-45's "by construction" claim held only for the
    *default* sections; it is now enforced against an explicit override.
  - **Authority consistency (D-46, refines D-44):** `POST /worlds` (create) and `POST
    /campaigns/{c}/time-skip` were any-authed but are the same structural timeline write as their
    operator-only siblings (`import` / fork time-skip / `end`) — both are now operator-only; time-skip
    also caps `days`. `start_campaign` gains the self-or-admin scope check (a player can no longer
    bind a PC naming another participant); `time-skip`/`dry-run` gain the minted-token campaign-scope
    check the WS/outcome paths already enforced.
  - **Error contract:** a negative `?limit` (parsed fine, then `LIMIT -1` → Postgres 500) and a
    non-int `seed` (a JSON array → `TypeError` escaping the catch → 500) now return `400`; `GET
    /campaigns/{c}/roster` returns `404` for an unknown campaign (was `200`).
  - **Resource / disclosure:** pack upload now caps *decompressed* size (a zip-bomb slipped the
    compressed-byte cap); `beat_failed` broadcasts a generic reason instead of the raw exception
    string (info disclosure across participants); the mechanics RNG `seed` is stripped from a
    player-facing campaign read (predictable combat); the Content-Length middleware docstring no
    longer overclaims (the decompressed cap is the real bound). All covered by new tests.
- **WS play-channel wire contract reconciled with `docs/08` (BE-11, #43)** — `docs/08` advertised
  server frames the handler never emits (`scene_update`/`mode_change`/`mechanics_result`/
  `suggestions`), client frames it doesn't accept (`encounter_action`/`pin_actor`), and a universal
  `campaign_id`/`beat_id` envelope that no frame carries. Corrected to the **real** contract,
  frame-for-frame: a table of the 3 accepted client frames + the 14 broadcast server frames (incl.
  the previously-undocumented `outcome_recorded` Chronicler frame) with their exact fields, and the
  non-emitted frames moved to an explicit "future GROW" note (scene/mode is the highest-value add but
  needs the pipeline to surface within-beat mode state; `suggestions`/`beat_id` need `run_beat` to
  yield structured frames, not only narration strings). Added transport tests asserting each frame's
  exact shape + that unknown client frames are silently ignored. No behavior change — the frames were
  always these; only the docs were wrong. (The uro-loom console's `docs/02` wire-drift note can now
  be dropped.)
- **PyPI publish workflow** — split into one job per package, each in its own GitHub environment
  (`pypi-core` / `pypi-server` / `pypi-cli`). A PyPI *pending* trusted publisher must be unique on
  `(owner, repo, workflow, environment)`, so all three sharing `environment: pypi` collided on setup.
  Owner setup updated in `docs/14` → "Publishing to PyPI". (D-43)

## [0.2.0] - 2026-07-19

### Added
- **Quantified/relational reaction triggers (RL-6, #25)** — a reaction rule can now react to "ANY
  member of faction X dying" (and the whole `$trigger`-bound predicate family): a `when` condition's
  entity-ref slots may be `$trigger.<field>`, bound from the triggering event's payload
  (`edge_exists(src="$trigger.actor_id", rel=member_of, dst=f:red-band)`). `when` is evaluated **per
  matching trigger event**, so it is a true existential over a multi-death beat — the naive
  "bind the first event" shape (which a design-check adversary showed misfires when a non-member dies
  first) is avoided. `trigger.per_event: true` fires the rule once **per** matching event (the
  count-each shape — e.g. `adjust_counter` per dead member; event-sourced, so it rides `fork_branch`,
  unlike a shadow game-code counter). Fences: `$trigger` is legal only in a ref slot of a rule (never
  a literal value slot or an agenda rule) and its field is validated against the event catalog at
  parse (the same check that fences `trigger.where`, and it must be a *string* field, not a list);
  an unbound-or-null ref fails the **whole** `when` closed. Read-only, so the action fence is
  untouched. `per_event` fan-out is bounded by the existing per-pass action cap (over-cap actions
  are audited). `RULES_API_VERSION` 4→5 (v1–v4 packs byte-identical). Resolves Ironwake's
  quantified-trigger gap-report row. (D-42)
- **Participant memory (B8, #7)** — a player's out-of-world notes that **survive a fork** (time-loop /
  roguelike / NG+): a caller-owned `ParticipantMemory` lane keyed on `(participant_id, world_ref)`,
  deliberately outside the branch/projection axis so `fork_branch` never resets it. Surfaces to the
  narrator as the player's private recollection (never canon / never an NPC belief, by direct
  construction); `uro codex add/list`; migration 017. The event-sourced journal is reserved (D-36).
- **Client-supplied plan (B9, #8)** — `Engine.run_beat/run_beat_stream/preview_beat(..., plan=BeatPlan)`
  drives the planner→mechanics gate **deterministically, with no LLM** (CI mechanics coverage + keyless
  consumers): free-roam checks *and* full encounters resolve from an injected plan. The supplied plan is
  fenced by the same `validate_plan` as an LLM plan (unknown affordance → `PlannerError`) and is a
  **trusted in-process input** — no D-32 ceiling (that fences the external Chronicler POST); a future
  network-exposed `plan=` must add it (D-37). `BeatResult` gains `check_traces` (per-check detail — incl.
  a resolved fight's rounds). Library API only for now (not wired to `serve`/CLI).
- **Arbiter shapes beyond round-robin (B7, #9)** — two new multiplayer turn shapes behind the same
  `TurnArbiter` port, plus a **non-canon coordination lane**: `uro serve --arbiter proposal` (a
  non-holder's intent is surfaced to the table as a proposal via the now-live `AdmitDecision.QUEUED`,
  not a silent refusal) and `--arbiter vote` (a session-only consensus tally). `uro connect` gains
  `/say <text>` (table-talk) and `/vote <choice>` — both **non-canon by construction** (broadcast via
  the session hub, never reaching a commit), so no proposal/debate/vote burns a canonical beat. New WS
  frames: `table_talk`, `proposal_opened`, `vote_tally`, `vote_decided`, `vote_unsupported`. All
  session-only (D-31), zero events/migrations. Consensual-PvP / simultaneous / reactive-interrupt stay
  reserved behind the same port (D-38).
- **Computation layer C3/C4/C5 — `for_each`, `roll_table`, `expire_claims` (#13)** — the reaction-layer
  rule grammar (`RULES_API_VERSION` 3→4; v1–v3 packs stay valid) gains: **`for_each`** (one bounded loop
  over a ref's edge-neighbors, with `$trigger.<field>` binding + `as`-var substitution, each neighbor
  scope-fenced); **`roll_table`** (a seeded, deterministic weighted pick → a chosen outcome's nested
  actions, baked so replay re-picks identically); and **`expire_claims`** (rumor decay — retract a stale
  module rumor to `truth=false`, migration 019 adds `created_day` to claims). All recursion is
  bounded (nested lists capped at parse; a shared per-pass node budget). Trust fences: a rule can never
  retract canon or reach a ref outside its scope through a binding. C6 (cascade) + computed-delta
  arithmetic stay staged (docs/19). (D-34)
- **Chronicler trusted-embedder ingestion tier + untrusted-path hardening (B6, #12)** — a Posture-A
  library embedder (which already holds root via `append_beat`) can now reuse outcome distillation
  *without* the D-32 protection ceiling via `uro_core.authored.distill_authored_outcome`, so an
  authored protected (T2+/PC) death commits as **real canon** and fires succession rules — unblocking
  assassination/succession games. **Trust is which module you import, never a wire flag**: an
  import-linter fence forbids the server from importing the ceiling-off path, and `OutcomeBundle` is
  now `extra='forbid'` + version-pinned (a forged `{trust:…}` field or unknown `v` → a loud 400).
  Untrusted-path hardening: an out-of-cast casualty now **drops** (was a public rumor); loot `to_ref`
  is protected (a bundle can't make a PC/named actor the recipient); the outcome endpoint enforces
  the D-39 campaign-scoped token. The untrusted parked-encounter registry stays reserved. (D-41)
- **Reaction-layer multi-ref scopes + dropped-action audit (B11, #11)** — a rule `scope` may now name
  several entities of one category (`factions: [a, b]` unions their members), the least-privilege
  middle ground between a single faction and the whole-`world` scope; a validator enforces exactly one
  jurisdiction. The action fence is unchanged (jurisdiction widened only). The rule gauntlet now
  produces a **dropped-action audit**: every refused action (out of scope, nonexistent target, a
  partial subject/witness filter, or over the per-pass cap) is recorded with a reason and logged —
  before, a fenced action vanished silently and an author couldn't tell a working rule from a no-op.
  `rules_api_version` bumped 2→3 (v1/v2 packs stay valid; a multi-ref pack declares v3). (D-40)
- **Session lifecycle: durable turn order + runtime tokens (B10, #10)** — refines D-31 (does not
  reverse it). Multiplayer **turn order is now reconnect/restart-stable**: the arbiter ring is seeded
  from durable PC-binding order (`store.pc_seats`, recovered from the `PCBound`/`PCReleased` log) — the
  turn cursor stays session-only (a durable cursor would ride `fork_branch`). **Runtime auth**: a
  durable, hashed (`sha256`), revocable, **campaign-scoped** `session_tokens` registry (migration 018,
  off the branch axis) lets a player added to a *running* server authenticate without a restart —
  minted via the authed `POST /join` (now returns `{actor_id, token}`), `POST /campaigns/{c}/tokens`,
  or `uro token mint`; revoked via `/tokens/revoke` or `uro token revoke`. `uro serve` gains
  `--admin-token` (an operator tier, distinct from ordinary `--token` players — only operators may act
  for others) and decouples `--arbiter` from the launch token count (so a runtime-added player still
  gets turns). New WS reject code `4403` (a minted token used on the wrong campaign). (D-39)
- **Small P3 follow-ups (B4/B5, #14)** — two evidenced, low-risk gap closures. **B4:** structured
  recall now surfaces a claim whose subject is an on-stage **place or faction** (`relevant()` unions
  on-stage `place_id`/`faction_id`), so a reaction-layer module rumor carrying a bare `p:`/`f:` ref
  (never a `name:` token) actually reaches the narrator when its entity is on stage — closing the
  P4×P9 seam. **B5 follow-up:** `store.current_world_time_batch(branch_ids)` returns each branch's
  in-fiction day in one recursive CTE (seeded from every head, carrying its origin), and `uro branch
  list` now prints each branch's `day=`. A third candidate — a fork-relative snapshot cadence for
  Hollowloop G-4 — was built, reviewed, and **reverted as a validated deferral**: the existing
  absolute cadence already bounds materialization replay to < N in every topology (`docs/18`).

### Changed
- **Repositioned Uro as its own thing — "a world-state engine" — and retired the git→GitHub analogy**
  from all outward-facing framing (README, `CLAUDE.md`, social preview, docs 00/01/03/08/10/glossary).
  New tagline: *"Worlds that remember. Timelines that fork."* The analogy was a comprehension scaffold,
  not the product's identity (D-35, refines D-1; D-1/D-30 keep their historical text).
- Added the logo as the README header + a branded 1280×640 social-preview image
  (`docs/images/`, regenerable from `social-preview.html`).

### Added
- `py.typed` markers on all three packages (PEP 561 — ship types to embedding consumers).
- Public package metadata in each `pyproject.toml` (`[project.urls]`, authors; keywords on uro-core).
- Least-privilege `permissions: contents: read` on the CI workflow.
- **Docker-first quickstart (#15, D-43)** — running any `uro` command against an unreachable
  database now prints one actionable line (`docker compose up -d --wait`, host port 5433, then
  `uro db migrate`; `URO_DATABASE_URL` to point elsewhere) instead of a raw driver traceback, via a
  single `connect_store` wrapper. Postgres + pgvector stays the one store — no second backend.
- **Dependency extras (#15, D-43)** — `uro-core`'s base install is now the pure engine (it imports
  only ports); the bundled adapters move behind extras: `uro-core[postgres]` (the Postgres + pgvector
  store), `uro-core[llm]` (the LLM provider adapters), `uro-core[all]`. `uro-cli` and `uro-server`
  pull both. Verified in a clean venv: the base install carries none of `asyncpg`/`pgvector`/`httpx`.
- **PyPI publishing plumbing (#15, D-43)** — an owner-activated `publish` workflow (trusted
  publishing / OIDC, no stored token) builds and uploads all three packages together; wheels now ship
  the LICENSE (`license-files`) and a per-package README as the PyPI long description. One-time
  pending-publisher setup and the run order are in `docs/14` → "Publishing to PyPI".

### Fixed
- Reconciled `docs/16-honesty-ledger.md` and `docs/10-roadmap.md` to cover Phase 10 (the computation
  layer, D-34) — they had stopped at Phase 9 and still described counters as "refused".
- README "four provider adapters" → accurate (three adapters / four provider kinds); added a
  `just`-less Quickstart fallback and tagged shell code fences as `sh`.
- Regenerated `uv.lock` (was stale at 0.0.1 after the 0.1.0 bump).
- Removed the dead GitHub-Discussions contact link's leaked TODO; enabled Discussions +
  private-vulnerability-reporting + secret-scanning on the repo so SECURITY.md's channel works.
- `just release` now checks all three package versions, not just uro-core.
- Guarded a stray root `data` file (PII) in `.gitignore`.
- `__version__` (and so `uro version`) now reads from installed package metadata on all three
  packages — it was hardcoded `0.0.1` and had drifted from the `0.1.0` in each `pyproject.toml`.

## [0.1.0] - 2026-07-13

First tagged release — the complete proof of concept, made public. The dated **development
milestones** below (P1–P10) are the pre-1.0 history folded into this release.

### Added
- The full engine: five PoC phases + five post-PoC phases (P1–P10) — see the milestones below.
- Public-readiness: MIT `LICENSE` + `license = "MIT"` in every package's metadata; community
  health files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue & PR templates);
  README badges + an architecture/tests overview; a release workflow + a documented versioning
  and release process.
- **G-3 — reproducible mechanics RNG:** `_beat_rng` derives from the campaign's persisted seed +
  commit depth (both deterministic) instead of random ids, so a played-through fight replays
  given the same event log. `uro campaign new --seed`; seed persisted (migration 016).

### Removed
- The initial LLM deep-research report was removed from the repo (and its history) before going
  public; the `docs/` set is the single source of truth.

## Development milestones (pre-1.0)

## 2026-07-12 — Post-PoC: the computation layer + the dogfood backlog

### Added
- **Phase 10 — computation layer (D-34):** engine-owned, event-sourced integer counters
  (`CounterChanged` → `proj_counters`, migration 015) that **fork by construction**, plus
  cross-entity compare and edge-count conditions; a `world` scope.
- **`Engine.append_and_react`** — the one-call authored-commit path (commit + react).
- Four games built **on** the engine as forcing functions, synthesized into an evidence-backed
  backlog ([`docs/18-gap-findings.md`](docs/18-gap-findings.md)).
- Backlog items: place-state recall (B4), cross-branch query surface (B5), a Chronicler
  ingestion receipt (B6), and an authed REST management surface (B3).

### Fixed
- A fork hot-path sequential scan (missing `memory_index(commit_id)` index, migration 014), a
  `PartyArbiter` holder-disconnect misrotation, accepted-but-inert rule triggers, and silent
  rule-pack death — all surfaced by the games.

## 2026-07-10 — Post-PoC: the reaction layer (D-33)

### Added
- **Phase 9 — reaction layer:** pack-authored reactive behavior as *declarative data*
  (`rules.yaml` / `agendas.yaml`), never code — a closed grammar that structurally cannot name a
  mechanical, lethal, or protected-canon event; a post-beat `react()` hook and a downtime
  `agenda_tick`.

## 2026-07-08 — Post-PoC phases 6–8 + live validation

### Added
- **Phase 6 — the alien ruleset (D-30):** a non-d20 second built-in (`uro_pbta`) through the
  same ruleset port, proving it game-agnostic.
- **Phase 7 — multiplayer (D-31):** per-participant PCs + a round-robin `PartyArbiter` behind
  the `TurnArbiter` port.
- **Phase 8 — Chronicler ingestion hardening (D-32):** `distill_outcome` is trust-scoped — an
  external bundle can't kill/loot/first-hand-witness a PC or a T2+ actor; idempotent replays.
- Per-role model routing; the honesty ledger ([`docs/16`](docs/16-honesty-ledger.md)).

### Verified
- The core thesis was validated live (with caveats) via an ablation run; the alien-ruleset and
  Chronicler legs were validated end-to-end.

## 2026-07-07 — The five-phase PoC is code-complete

### Added
- **P1 state engine** (recall → narrate → extract → gauntlet → commit → project; claims/beliefs;
  pgvector memory), **P2 branching timelines** (the meteor test), **P3 mechanics** (ruleset port
  + Uro Basic d20; seeded encounters), **P4 worlds** (packs, import, procedural history seeding,
  probes), **P5 server & federation** (WS play, export/import with hash-chain verification,
  belief propagation, Chronicler mode — the war-story test).

## 2026-07-04 — Scoping

### Added
- Initial design: re-scoped from a consumer *platform* to a headless *engine* (the engine/platform
  boundary). Decisions D-1…D-15 (see [`docs/decisions.md`](docs/decisions.md)).

[Unreleased]: https://github.com/cupskeee/uro/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cupskeee/uro/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cupskeee/uro/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cupskeee/uro/releases/tag/v0.1.0
