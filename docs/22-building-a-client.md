# 22 — Building a client (a primer)

For a frontend engineer building a web client on top of Uro, working on a machine with this repo
checked out. It assumes the product shape we're building:

- **No login.** Whoever is at the keyboard plays. There are no accounts; the client holds one token.
- **Bring your own model.** No credits: the player connects their own LLM provider in the client
  (an API key, a local model, or a ChatGPT subscription), through Uro's provider registry.

Everything below was run against this repo; the commands are the real ones. Play is supported in
English for now (D-101).

## What Uro is, and isn't

Uro is the **world engine**: it keeps a persistent, branching world (who exists, what happened,
who knows what), runs the LLM calls that narrate and record each turn, and applies a game's rules.
It speaks HTTP + WebSocket. It is **not** the product: no UI, no user accounts, no images or
voice, no content moderation, no billing (it meters tokens; it never prices them). All of that is
the client's.

## The concepts the UI shows

| Concept | What it is | In the UI |
|---|---|---|
| **World** | A setting: places, people, factions, plots, tone, a ruleset. Created from a *world pack* (`worlds/ashfall`, …). | world picker, world page |
| **Campaign** | One playthrough in a world, with the player's character (the **PC**). | "your games", resume |
| **Beat** | One turn: the player's free-text **intent** ("I ask Mera about the boats") → streamed **narration** → the world updates. | the play screen |
| **Branch / fork** | Worlds are versioned like git: fork a world at any point into a "what if" timeline, or skip time ("a year later"). | a timeline / "what if" feature |
| **Time** | An in-fiction day counter. Moves when the player sleeps or travels, or on a time-skip. | a day badge |
| **Knowledge** | Facts (`claims`, some true, some rumor) and who believes what (`beliefs`). Characters can be wrong or lie. | a journal, NPC panels |

Vocabulary: `docs/glossary.md`. Flows end to end: `docs/15-walkthroughs.md`.

## Run it on this machine

Prerequisites are installed here: Docker, `uv`, Node with `npm`/`pnpm`, and the `claude`/`codex`
CLIs. From the repo root:

```sh
uv sync --all-packages                      # once
docker compose up -d --wait                 # Postgres + pgvector, on host port 5433 (not 5432)
```

**Use your own database.** The default `uro` database is the engine's shared dev instance (well
over a hundred test worlds). Make one for the client, so your worlds stay yours:

```sh
docker compose exec postgres psql -U uro -c "create database uro_loom"
export URO_DATABASE_URL=postgresql://uro:uro@localhost:5433/uro_loom   # every uro command below reads this
uv run uro db migrate                       # creates the tables (and the vector extension)
uv run uro world create worlds/ashfall      # import a starter world (also: brinehold, emberfell, thornwood, and halcyon: a 2091 deep-sea station)
```

A fresh database has **no model configured**, so the server narrates with the built-in **stub**:
canned text, instant, free. That's ideal for building the UI. Real narration starts once a
provider is set up (below). `.env` at the repo root holds `URO_SECRET_KEY`, which the server needs
to store provider keys; the `uro` commands load it automatically. Never print it or commit it.

**Start the server** (keep it running in its own terminal):

```sh
uv run uro serve --port 8000 \
  --admin-token "$URO_TOKEN=player-1" \
  --cors-origin http://localhost:5173
```

- `URO_TOKEN` is any secret string you choose (put the same value in the client's `.env.local`).
  An `--admin-token` is an **operator** token that is also a player (`player-1`): one token does
  everything. That's the no-login model.
- `--cors-origin` lets the browser on Vite's dev port call the server. Without it, every call fails
  in the browser while `curl` works.
- The server binds `127.0.0.1` by default. Keep it there: the browser holds an operator token,
  which is fine on your own machine and never on a public deployment.
- Ports here: 8000 and 5173 are free. Some other ports on this machine belong to other services;
  check before picking another (`lsof -nP -iTCP:<port> -sTCP:LISTEN`), and never stop a process
  you didn't start.

**Generate the API types** (the server publishes a validated schema for every HTTP response):

```sh
npx openapi-typescript http://localhost:8000/openapi.json -o src/api/uro.d.ts
```

Browse it at `http://localhost:8000/docs`. The WebSocket frames aren't in OpenAPI; they're listed
below and in `docs/08`.

**Auth.** HTTP: `Authorization: Bearer <token>`. WebSocket: `?token=<token>` in the URL
(browsers can't set WebSocket headers).

## The core loop

```
GET  /worlds                                  pick a world: id, name, description, tone, content (GET /rulesets: each ruleset's sheet shape)
POST /worlds/{world_id}/campaigns             {"participant": "player-1", "new_pc_name": "Wren"}
                                              → {"campaign_id", "branch_id"}
GET  /campaigns                               resume: every campaign (?world_id= to filter)
GET  /campaigns/{c}/chronicle?limit=50        the story so far, oldest first: rebuild the transcript on load
WS   /campaigns/{c}/play?token=…              play
GET  /campaigns/{c}/state?sections=pcs,actors,places,threads,factions,sheets   side panels
```

**A beat over the WebSocket.** Send:

```json
{"type": "intent", "text": "I step into the Brinehouse and look around.", "id": "<your uuid>"}
```

You get, in order (every frame that settles your intent echoes your `id` as `intent_id`):

| frame | meaning | the UI |
|---|---|---|
| `beat_started` | the turn began | show "the world answers…" |
| `beat_checks` | (only when the beat has a check or a fight) `checks`, `mode`: what the rules decided, before the story; provisional until `beat_committed` | show the roll first (undo it on `beat_failed`) |
| `narration_chunk` × N | `text` pieces of the narration, as the model writes them | stream them into the transcript |
| `beat_narrated` | the story is fully told; the world is now being updated | re-enable input here, don't wait |
| `beat_committed` | saved: `narration`, `commit_id`, and what the beat did: `checks` (`{actor_id, affordance, outcome}`, the ruleset's grade), `suggestions` (for the acting `participant_id`), `mode` (`encounter` = a fight was resolved), `world_day`, `created` (new people, places, factions, plots), `threads_changed` (any plot whose state moved, including a dormant one a rule created, which you may hide until it wakes) | show the outcomes and what's new; update the day badge; offer suggestions only to the player who acted |
| `beat_failed` | `error` (generic) | show a retry; nothing was saved |

Also broadcast: `participant_joined`/`participant_left`, and for parties `not_your_turn`,
`proposal_opened`, `table_talk`, votes (`docs/08`). Rejections close the socket with
`4401` (bad token), `4403` (token not for this campaign) or `4404` (no such campaign).

**More to build on:**

| feature | call |
|---|---|
| player's private notes ("codex") | `GET`/`POST /campaigns/{c}/codex` `{text, key?, pinned?}` |
| "what if" fork from any point | `POST /worlds/{w}/branches` `{from_ref, name, time_skip_days?}`, then a campaign on it (`"branch": "<name>"`) |
| skip ahead | `POST /campaigns/{c}/time-skip` `{days, downtime}` (`downtime: false` = time passes in play, nothing rests) |
| preview a turn without saving | `POST /campaigns/{c}/dry-run` `{intent}` |
| a new game (its own timeline) | `POST /worlds/{w}/campaigns` `{participant, new_pc_name, own_branch: true}` (optionally `from`: a marker or commit, `branch_name`): forks the world at `main`'s head and starts the campaign there, in one call (D-100; operator token) |
| delete a playthrough (privacy) | `DELETE /campaigns/{c}` removes it and its branch for good; `DELETE /worlds/{w}` removes a whole world. A campaign on the world's `main` branch can't be removed alone, so start each separate game with `own_branch: true`. A campaign removal keeps history another branch shares (a fork made from it) and the player's codex notes for the world; only a world removal takes those |
| a plot's lifecycle | `state?sections=threads` gives each thread's `developments` (beats that brought it into play) and `beats_live`; `POST /campaigns/{c}/threads/{t}/state` `{state}` (operator token) lets your game open, park or end a plot (the world's rules react, D-102) |
| plots gone quiet (a journal's "loose ends") | `GET /campaigns/{c}/pacing`: each live thread's beats since it came up; a world that opts in (`[pacing]`) also has the engine nudge the quietest back into the story (D-95, D-96) |
| world history / timeline | `GET /worlds/{w}/branches`, `GET /worlds/{w}/log` |
| import a world pack | `POST /worlds/create` (multipart field `pack`: a `.zip` of the pack folder); check it first with `POST /worlds/validate` |

## Bring your own model: the settings screen

The provider registry lives in the database; the client edits it over HTTP (operator calls, which
your token is). Several providers can be connected at once, and each engine **role** is bound to
one connection + model.

**Provider kinds:**

| `provider` | what the player gives | notes |
|---|---|---|
| `openai` | an API key | chat + embedding models |
| `anthropic` | an API key | chat only (bind the embedder elsewhere) |
| `local` | a base URL (default `http://localhost:11434/v1`, Ollama) | any OpenAI-compatible local server |
| `openai_compat` | a base URL + key | any OpenAI-compatible service (OpenRouter, Gemini's compatible endpoint: untested) |
| `codex` | a ChatGPT subscription, by device login (no key) | uses the player's ChatGPT plan; chat only |
| `claude_cli` / `codex_cli` | nothing: the logged-in CLI on this machine | headless; about twice as slow; chat only |
| `stub` | nothing | free canned text, for development |

**Roles** (anything unbound uses `default`): `default`, `narrator` (the story), `extractor` (turns
the story into world state), `planner` (decides what a turn involves), `embedder` (long-term
memory — needs an **embedding** model; without one, recall of older beats is off), `dialogue`
(optional: NPC speech on another model), `judge` (capability probes). One chat model on `default`
plus an embedding model on `embedder` is a complete setup.

**The flow:**

```
POST   /providers/credentials      {"provider": "openai", "access_token": "sk-…"}    → {"id"}  (stored encrypted)
POST   /providers                  {"name": "My OpenAI", "provider": "openai", "auth_id": "<credential id>"}  → {"id"}
POST   /providers/{id}/refresh     list its models → [{id, modality: chat|embedding|unknown}]  (a live call)
POST   /providers/{id}/test        {"model"?} → {ok, detail}                                    (one tiny call)
PUT    /providers/roles/default    {"connection_id", "model", "reasoning_effort"?}
PUT    /providers/roles/embedder   {"connection_id", "model": "text-embedding-3-small"}       (an embedding model)
POST   /providers/reload           apply it now, no restart
GET    /providers                  the current setup (keys are never returned: has_access_token only)
```

A ChatGPT subscription: `POST /providers/codex/start` `{name?}` → show `user_code` and
`verification_uri`; poll `POST /providers/codex/poll` `{login_id}` every `interval` seconds until
`status: "connected"` (it returns the new connection and its models). A key goes over the wire
once, is encrypted at rest under `URO_SECRET_KEY`, and is never sent back.

**Cost the player can see:** `GET /usage?campaign={c}` gives calls and tokens per stage and model.
A beat makes several calls (planner, narrator, extractor, embeddings). Uro has no prices, so show
tokens, or let the player enter their own rates.

## Design for the engine as it is

- **Pacing.** With a real model the narration starts streaming after about 7 seconds and is saved
  a few seconds after it ends (`docs/21`). Stream it; free the input at `beat_narrated`. The stub
  is instant, so test with a real model before judging the feel.
- **One turn at a time per campaign.** Party play takes turns (round-robin, proposals or votes,
  `docs/08`).
- **Free text is the input.** `beat_committed` carries the planner's `suggestions` (a world with a
  ruleset), which a UI can offer as buttons that send the text as the next intent.
- **Don't spoil the game.** Your token can read everything (it's an operator token), including
  which claims are true, every character's beliefs, and the raw event log
  (`/worlds/{w}/events`). A player UI should show what the player's character would know: the
  narration, the people and places met, the plots in play, their own sheet and notes.
- **Not available yet:** a scene frame (the engine keeps no scene), images, voice. Ask if the UX needs one; it's
  engine work.
- **Errors:** `400` bad input, `401` bad token, `403` not allowed, `404` unknown id, `501` a feature
  not wired (e.g. storing a key without `URO_SECRET_KEY`), `502` the model provider failed.
- **Ids** are ULIDs: compare and display them in full.

## Reading list

`README.md` → `docs/00-vision.md` → `docs/glossary.md` → `docs/02-domain-model.md` →
`docs/15-walkthroughs.md` → `docs/08-api-and-sessions.md` (the full HTTP + WebSocket contract) →
`docs/09-world-definition.md` (world packs) → skim `docs/03` (branching), `docs/16` (what's built
and what isn't) and `docs/21` (latency). Skip `docs/decisions.md` and the engine internals.

And play it once in the terminal, which teaches the rhythm faster than any doc:

```sh
uv run uro campaign new <world-id> --pc "Wren"   # → a campaign id
uv run uro play <campaign-id>
```
