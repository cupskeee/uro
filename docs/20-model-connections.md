# 20 — Model connections (DB-backed, instance-level)

> **Status: accepted (D-47); slices 1–4 shipped (complete)** · Decision: [`D-47`](decisions.md) · Engine
> change: new operational tables (migration 020) + a registry adapter + a server/CLI surface; the
> core ring is **untouched**. Built: the registry substrate + encrypted-credential store +
> `build_router_from_registry` + `serve` resolution + the `uro provider` CLI (slice 1); the
> operator-only `/providers` HTTP surface so uro-loom/any client configures it (slice 2). Slices 3–4 (model discovery/validation via `refresh`/`test`, reload-without-restart) are
> also built.

Move LLM provider configuration out of the deployment file (`uro.toml [llm.roles]` + `--provider`
flag + env keys) and into the **database**, as an **instance-level** registry configurable over the
`uro-server` API — so `uro-cli`, `uro-loom`, and any third-party API client edit the same source of
truth, and one instance can have several providers connected at once.

## The principle

**A model connection is a database row, not a code path.** Adding a *connection* to an existing
provider kind is an `INSERT`; adding a new provider *kind* (e.g. `google-genai`) is one adapter
implementing the port. Everything else — the tool loop, event ordering, retries, cancellation,
streaming — exists exactly once and is unchanged.

Uro already embodies this: `providers/base.py` `LLMProvider` (`stream`/`complete`/`embed`) is the
port, and `providers/router.py` `ProviderRouter(bindings, default)` is the "exists exactly once"
seam. Today `wiring.py` builds that router from the file+flags+env. This change makes the router's
bindings come from DB rows instead. **The engine core never sees any of this** — it resolves
providers by role through the port, exactly as now.

## Scope: instance-level, operational — NOT world-state

Model connections attach to the **uro instance** (the server process + its Postgres), not to a
world, campaign, or branch. Consequences, all deliberate:

- **Not event-sourced.** These are plain operational tables, off the event/branch axis — they never
  become domain events or projections, never fork with a branch, never enter an `export_world`
  bundle. Precedent: the `session_tokens` registry (migration 018) is exactly this shape.
- **One router per instance.** Because connections are instance-scoped, there is a single router to
  resolve — this sidesteps the "one provider per process" per-campaign-rebind deferral entirely.
  Reconfiguring rebuilds that one router (slice 4); per-campaign model selection stays out of scope.
- **Secrets in the DB are safe here.** The export-leak worry that applies to world-state does *not*
  apply: instance config is never in a bundle. Securing the DB (plus the KEK, below) is the boundary.

## Schema (migration 020 — three tables, forward-only)

### `model_connections` — one row per connected provider endpoint

| Column          | Type          | Null / constraint                     | Notes |
| --------------- | ------------- | ------------------------------------- | ----- |
| `id`            | `TEXT`        | PK                                    | ULID (matches world/campaign id convention) |
| `name`          | `TEXT`        | NOT NULL                              | display label |
| `provider`      | `TEXT`        | NOT NULL                              | discriminator: `anthropic` \| `openai` \| `openai_compat` \| `local` \| `codex` \| `claude_cli` \| `codex_cli` \| `stub` → an adapter (the two `_cli` kinds run a local CLI, see *Headless CLI kinds*) |
| `base_url`      | `TEXT`        | NULL                                  | per-connection endpoint override (an OpenAI-compatible or self-hosted URL); for `claude_cli`/`codex_cli`, the CLI's executable path |
| `auth_id`       | `TEXT`        | NULL, FK → `provider_credentials(id)` **ON DELETE SET NULL** | soft-linked credential |
| `is_enabled`    | `BOOLEAN`     | NOT NULL DEFAULT true                 | |
| `cached_models` | `JSONB`       | NULL                                  | `[{"id":"gpt-4o","modality":"chat"}, {"id":"text-embedding-3-small","modality":"embedding"}]` — discovered by `refresh` |
| `created_at`    | `timestamptz` | NOT NULL DEFAULT now()                | |
| `updated_at`    | `timestamptz` | NOT NULL DEFAULT now()                | |

- **`cached_models` carries `modality`** (`chat` \| `embedding` \| `unknown`), classified per-adapter
  on refresh (see *Model discovery*). It stays a JSONB column, not a fourth table — `role_bindings`
  is tiny and instance-scoped, so bindings validate against this JSON at write-time; promote to a
  `provider_models` table only if per-model pricing/context-window/queryable metadata is later needed.
- `disabled_models` was considered and **dropped** — hiding models is not a needed concern; a disabled
  *connection* (`is_enabled=false`) is the coarse control that matters.

### `provider_credentials` — credentials, encrypted at rest

| Column          | Type          | Notes |
| --------------- | ------------- | ----- |
| `id`            | `TEXT`        | PK — ULID |
| `provider`      | `TEXT`        | which provider kind this credential is for |
| `access_token`  | `TEXT`        | **ciphertext** — app-level Fernet, decrypted only in the adapter (see *Credentials & the KEK*) |
| `refresh_token` | `TEXT`        | NULL — ciphertext (OAuth) |
| `last_refresh`  | `timestamptz` | NULL |
| `auth_mode`     | `TEXT`        | `api_key` \| `oauth_pkce` \| `oauth_device` — tells the runtime how to turn this row into a bearer token |
| `created_at` / `updated_at` | `timestamptz` | |

Split from `model_connections` on purpose: one credential can back several connections, and the
auth lifecycle (refresh/expiry/re-auth) is independent of the model catalog. `auth_mode` discriminates
the runtime token resolver — a separate question from *which provider* the credential belongs to
(`provider`). The link is one-directional and **`ON DELETE SET NULL`**: deleting a credential
*unlinks* the connections that used it (they become unauthenticated until re-linked), never deletes
them, and a connection can't point at a credential id that doesn't exist while linked. That gives the
"no surprise cascade" property with referential integrity — a strict improvement over a soft link that
allows dangling `auth_id`s and orphaned live secrets.

> Naming: `provider_credentials` (not `provider_auth_sessions`) to avoid colliding with the existing
> `session_tokens` (D-39, participant auth) — these are stored credentials, not sessions.

### `role_bindings` — which connection+model backs each engine role

| Column          | Type   | Null / constraint | Notes |
| --------------- | ------ | ----------------- | ----- |
| `role`          | `TEXT` | PK                | `default` \| `narrator` \| `extractor` \| `planner` \| `embedder` \| `dialogue` \| `judge` |
| `connection_id` | `TEXT` | NOT NULL, FK → `model_connections(id)` **ON DELETE CASCADE** | |
| `model`         | `TEXT` | NOT NULL          | one of the connection's `cached_models` ids |
| `reasoning_effort` | `TEXT` | NULL          | how hard a reasoning model thinks (`none`…`xhigh`, D-71, migration 026); NULL = the provider's default. Sent by the codex and OpenAI-style adapters only |
| `updated_at`    | `timestamptz` | | |

This is the piece the two catalog tables don't cover: the engine resolves providers **by role**
(verified in `engine.py`/`router.py` — `narrator`/`extractor`/`planner`/`embedder`/`dialogue`/`judge`),
so without this mapping the router has nothing to bind. `role` is the PK (one binding per role per
instance). The reserved `default` role IS the `ProviderRouter` default (the fallback for any unbound
role) — so the router maps cleanly to `(bindings = the other roles, default = the "default" role)`.
`ON DELETE CASCADE`: deleting a connection unbinds its roles, which then fall back to `default` — the
expected, non-surprising direction (you removed the thing they pointed at).

## Credentials & the KEK (app-level encryption)

`access_token`/`refresh_token` are stored **encrypted**, decrypted only inside the credentials
adapter, using a **single key-encryption-key that lives OUTSIDE the DB** — `URO_SECRET_KEY` in env
(a Fernet key via the `cryptography` lib; the columns are plain `TEXT` ciphertext, since the store is
asyncpg + hand-written SQL, not an ORM).

Why the KEK must be external: if the key lived in the DB, a dump would reveal everything and
"encrypted at rest" would be theatre. With one env KEK, an accidental **backup, replica, or log dump**
(the #1 real-world leak vector) is useless without it — while everything else stays in the DB exactly
as intended. Fail-closed: if `URO_SECRET_KEY` is unset, credential *storage* is refused (the registry
can still run stub/keyless connections), so a misconfigured instance never silently persists plaintext.

This is the whole security story for the instance-level case: the operator configuring the instance is
already the trusted `is_admin` tier (D-44), so an encrypted-in-DB key is appropriate — no external
secret manager required, per the owner's call.

## Model discovery & embedder validation

`POST /connections/{id}/refresh` fetches the provider's model list and writes `cached_models`. There
is **no universal cross-provider "is embedding" flag**, so each adapter classifies its own models into
`modality`: OpenAI → `text-embedding-*` = embedding; Anthropic → none (chat-only); Google →
`*-embedding-*`; local/Ollama → a configured hint; unknown provider → `unknown`.

Binding the `embedder` role is then validated in two layers:
- **Write-time (fast path):** the API rejects binding `embedder` to a `modality != "embedding"` model
  with a 400; `modality: "unknown"` is allowed with a warning (don't hard-block a provider we can't
  classify).
- **Definitive backstop:** `POST /connections/{id}/test` (and optionally the embedder bind) does a
  live 1-token `embed` call — if the model isn't an embedder the provider itself 400s. That's the only
  *certain* check; classification is the cheap fast-path.

## Resolution & precedence

The server builds its `ProviderRouter` from the registry: `default` role → the router default, the
other role bindings → `bindings`, each resolving `connection → adapter (by provider kind) + base_url +
decrypted credential + model`. Precedence, highest wins:

**DB registry (if non-empty) → `uro.toml [llm.roles]` → `--provider` flag → stub default.**

So the file+flag path is demoted to a **bootstrap seed** used only when the registry is empty — the
embedded / single-operator / `uro play` path keeps working untouched, and a fresh instance still comes
up (on stub) with no config. Adding the first connection via CLI/API takes over.

## Surfaces

- **`uro-server` API (operator-only, D-44 — config is a cost/structural concern):**
  `GET/POST/PATCH/DELETE /providers` (connections), `GET/POST/DELETE /providers/credentials`,
  `GET/PUT /providers/roles` (bindings), `POST /providers/{id}/refresh`, `POST /providers/{id}/test`.
- **`uro-cli` (direct-DB, like `uro world`; operator with DB access):** `uro provider
  add|list|rm|enable|disable`, `uro provider bind <role> <connection> <model> [--reasoning-effort low]` / `unbind <role>`,
  `uro provider refresh|test <connection>` (`test --reasoning-effort <level>` checks a model accepts that effort, D-71), `uro provider link <connection> <credential>` / `unlink`,
  `uro provider codex-login`, and `uro provider credential add|rotate|rm` (`rotate` replaces a key in
  place, so every connection linked to it uses the new one; `rm` refuses a credential a connection still uses unless `--force`, which unlinks those
  connections; deleting a connection keeps its credential, since one may back several). Refresh
  and test are the same core functions the endpoints call (`refresh_connection_models`,
  `probe_connection`). These commands edit the database: a RUNNING server sees the change after
  `POST /providers/reload` — `uro provider reload --server URL --token T` — or a restart.
- **`uro-loom`:** the same API — a non-technical operator connects a provider, pastes a key (stored
  encrypted), picks models, and binds roles, all in the browser. (This is the instance operator's
  key; *per-end-user* keys in a multi-tenant deploy remain a platform/BFF concern, `docs/05` of Loom.)

## Headless CLI kinds (D-72)

Two kinds run a locally installed, logged-in CLI once per call instead of calling an HTTP API:
`claude_cli` runs `claude -p` (Claude Code) and `codex_cli` runs `codex exec` (Codex). The
**credential is the CLI's own login** on the machine running Uro (a Claude or ChatGPT subscription,
or whatever the CLI holds), so the connection links no credential; `base_url`, if set, names the
executable (default: found on `PATH`). Its file name must be `claude` or `codex` (`.exe`/`.cmd` on
Windows): it picks WHICH install runs, never what program. The model is any id the CLI accepts
(`claude_cli` lists the `haiku`/`sonnet`/`opus` aliases; `codex_cli` lists `default`, Codex's
built-in default, since the user's Codex config isn't read), a role may carry a reasoning effort
(`--effort`, which takes `low`…`max`; `model_reasoning_effort`), and neither embeds. A call times out
after 300 s spent waiting on the CLI; a slow reader of the narration doesn't count against it.

```
uro provider add claude --provider claude_cli
uro provider bind narrator <connection> sonnet
uro provider add codex-cli --provider codex_cli
uro provider bind extractor <connection> gpt-5.6-luna --reasoning-effort low
```

**Locked down.** Both CLIs are coding agents with tools, and player text reaches their prompts.
Every call runs in a fresh, empty directory that is removed afterwards, with the prompt on stdin
(never in `argv`, where `ps` shows it) and Uro's system prompt from a file beside that directory, in
place of the agent's own. A multi-turn prompt (a re-ask) is written as a transcript whose turn
markers carry a random token per call, so player text can't forge a turn. The CLI gets only the
environment it needs to find its own login and the network (`PATH`, `HOME`, the user and locale,
`TMPDIR`, `CODEX_HOME`/`CLAUDE_CONFIG_DIR`, `XDG_*`, proxy and CA settings). Otherwise an
`ANTHROPIC_API_KEY` in Uro's environment would take precedence over Claude's subscription login
(checked live), and `URO_SECRET_KEY` and the database URL would be readable by the agent. So a CLI
that authenticates through environment variables (an API key, Bedrock, Vertex) won't log in here;
those are what the HTTP kinds are for.

- `claude -p` with `--tools ""` (Claude Code's built-in tool set emptied), `--strict-mcp-config` (no
  MCP servers), `--setting-sources ""` (no user or project settings, so no hooks),
  `--no-session-persistence`, `--disable-slash-commands`. (`--bare` is not used: it reads only an
  API key, never a login.)
- `codex exec` with `--sandbox read-only`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`,
  `-c web_search="disabled"` and a `--disable` for each Codex 0.163 feature that gives the agent a
  tool or runs something on its own (`CODEX_DISABLED_FEATURES` in `providers/adapters/cli.py`: the
  shell and `unified_exec`, image viewing, browsers and computer use, image generation, apps and
  plugins, sub-agents, sleep, goals, skills, tool suggestions, hooks and the code-mode host).
  Measured live (2026-11-01): with only the read-only sandbox, `codex exec` ran `sed` on a planted
  file outside its directory and quoted it back; with the shell tool off it could not. Codex accepts
  `--disable` of a RETIRED feature name and does nothing, so before its first call the adapter reads
  `codex features list` and refuses to run if any of those features is gone or `removed`. A tool
  added under a new feature name in a later Codex is not on the list; the backstop is that any tool
  event in the output (anything but a message, reasoning or a notice) makes the call fail.

The operator still chooses which `claude`/`codex` runs and what that login can reach; the
connection endpoints are operator-only (D-44).

**Costs.** Measured live on a three-beat Ashfall session (narrator and planner on `claude_cli`,
extractor on `codex_cli` at `low`): each call pays ~1.5–3 s of CLI start-up, so the player could type
again after 10–13.5 s and a beat was saved after 17–26 s, about twice the HTTP providers' times
(`docs/21`). `codex exec` sends whole messages, so narration through it doesn't stream, and each call
carries ~7,000 tokens of Codex's own harness (mostly cached). For a ChatGPT subscription, the `codex`
kind (direct HTTP with its own login) is faster and streams; `claude_cli` is the way to play on a
Claude subscription without an API key.

## Hexagonal fit

A DB-backed `ProviderRegistry`/`ModelConnectionStore` adapter (in the adapter/wiring layer) reads the
tables and builds the router. The **core ring imports only the `LLMProvider` port + `ProviderRouter`**
— unchanged, and the import-linter contract stays KEPT. Router construction becomes dynamic (read DB)
instead of static (read file), but the seam the core sees is identical.

## What this unlocks now vs. deferred

- **Now:** model-agnostic; several providers connected at once; role→model bindings; edit from CLI,
  Loom, or any API client; encrypted operator keys (so Loom *can* accept a pasted key at the instance
  level from v1, because the encrypted store exists).
- **Deferred (named):** live **per-campaign** model selection (needs the engine to resolve a router
  per campaign — the one-provider-per-process work, out of scope here); **per-end-user** credentials
  and org isolation (multi-tenant — rides the BFF/platform layer, Loom `docs/05`); OAuth runtime
  resolvers (`auth_mode` reserves the schema; `oauth_pkce`/`oauth_device` wire in when a provider
  needs them); further provider *kinds* (each is one adapter — a native `google-genai` is the
  obvious next).
- **Untested:** OpenRouter (`https://openrouter.ai/api/v1`) and Google Gemini
  (`https://generativelanguage.googleapis.com/v1beta/openai`) both offer an OpenAI-compatible
  endpoint, so an `openai_compat` connection with that `--base-url` and the service's key should
  reach them, but neither has been run against Uro.

## Build plan (slices, each `just test`-green + committed)

1. ✅ **Registry substrate (engine) — SHIPPED:** migration 020; the `provider_credentials` adapter (Fernet, KEK
   from env); a `ModelConnectionStore` port + Postgres adapter; `build_router_from_registry()` in the
   adapter layer; `serve` resolves the router from the DB with the file/flag/stub seed fallback; CLI
   `uro provider add/list/rm/bind`. Core untouched.
2. ✅ **Server API — SHIPPED:** the operator-only (D-44) `/providers` connections/credentials/bindings
   CRUD (`GET /providers` snapshot; `POST`/`PATCH`/`DELETE /providers[/{id}]`;
   `POST`/`DELETE /providers/credentials[/{id}]`; `PUT`/`DELETE /providers/roles/{role}`), so
   uro-loom / any client configures the registry. Credentials arrive as plaintext over the
   operator-only wire and are encrypted at rest; no read returns a secret. (`uro provider …`
   retargeting to a running server — like `uro token` — is a deferred nicety; the CLI stays direct-DB.)
3. ✅ **Discovery + validation — SHIPPED:** `refresh` (per-adapter model listing + modality), `test` (live probe),
   embedder write-time validation.
4. ✅ **Reload-without-restart — SHIPPED:** rebuild the single instance router on a registry change.
