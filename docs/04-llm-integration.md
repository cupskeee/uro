# 04 — LLM Integration

> The **role set** below is a living list (owner feedback: "not a fixed set"); the *ports* are the stable part.

## Design goals

1. Connect many kinds of models: cloud APIs, local Ollama, anyone's fine-tuned or OpenAI-compatible endpoint.
2. Route by **role**, not by hardcoded model — every world/deployment binds roles to models in config.
3. Make model compatibility **testable per world** (capability probes), instead of enforcing content policy in the engine.
4. Engine never fine-tunes anything; consumers may bind fine-tuned models like any other endpoint.

## Provider port

```python
class LLMProvider(Protocol):
    async def complete(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]: ...
    async def embed(self, texts: list[str]) -> list[Embedding]: ...
    def info(self) -> ProviderInfo   # model id, context window, declared capabilities
```

`CompletionRequest` carries messages, optional JSON schema (for structured output), temperature, seed-if-supported, and a `stage_tag` for usage metering. Structured output is first-class: pipeline stages that feed state (planner, extractor) *require* schema-validated responses with automatic re-ask on validation failure.

### Adapters (MVP set)

| Adapter | Covers | Auth |
|---|---|---|
| `openai_compat` | OpenAI, Ollama (`/v1`), vLLM, most gateways, fine-tuned deployments | API key (or none, local) |
| `anthropic` | Claude models | API key |

One adapter (`openai_compat`) covers the long tail of the ecosystem, including self-hosted Ollama. LiteLLM was considered as the universal adapter; decision is to hand-roll `openai_compat` + `anthropic` (small, and we need streaming + structured-output control) and revisit if the adapter zoo grows. Recorded in `decisions.md` (D-11).

### Auth strategies

Auth is a separate small interface (`acquire() / refresh() / headers()`) composed into adapters, so adding a new credential flow never touches pipeline code. MVP ships API-key (and no-auth for local endpoints) only. Subscription-OAuth adapters piggybacking on the Codex / Claude Code login flows were considered and **removed from scope** — they use credentials contractually scoped to those tools (ToS gray zone, breakable without notice). See `decisions.md` D-16; the auth-strategy seam keeps the door open if that ever changes.

### Credential handling (the data-privacy part the engine DOES own)

- Local mode: OS keychain via `keyring`, fallback to an encrypted file (age/Fernet, key outside the repo).
- Server mode: encrypted at rest, never logged, never exported in world/campaign packs, redacted from error traces.
- Story content never leaves the engine except to the models the consumer explicitly bound.

## Role-based routing

```toml
# per world or per deployment; world pack may suggest, deployment overrides
[llm.roles]
narrator   = "anthropic:claude-sonnet-5"       # scene prose, outcome narration
dialogue   = "anthropic:claude-sonnet-5"       # in-character NPC speech
planner    = "openai_compat:gpt-5.1"           # beat planning, structure decisions (needs strong structured output)
extractor  = "openai_compat:ollama/qwen3-14b"  # canonicalization: pull entities/claims from prose (cheap, high-volume)
summarizer = "openai_compat:ollama/qwen3-14b"  # memory compression, recaps
embedder   = "openai_compat:text-embedding-x"
judge      = "openai_compat:gpt-5.1"           # probe scoring (D-24); prefer judge ≠ judged
```

One physical model may serve all roles (cheap start); the indirection costs nothing and enables per-role optimization later. Roles are registered in code with a described contract (input shape, output schema, latency class) so new roles can be added without touching adapters.

**Overriding a role without editing config:** `uro play` / `uro dry-run` accept a repeatable
`--role-model role=spec` where `spec` is a provider spec (`openai:gpt-4o`) or a bare model
(`gpt-4o`, bound to the `--provider` kind). CLI overrides win over `[llm.roles]`; unlike a config
role (skipped-with-warning if its key is missing), an explicit override that can't be built raises
— you named it, so a silent fallback would mislead. (A bare model with a colon in its own name,
e.g. an Ollama tag `llama3.1:8b`, must use the full form `local:llama3.1:8b`.)

**Live-validated cost split (2026-07-09, docs/16):** the planner needs a strong model — on
gpt-4o-mini it fired a PbtA encounter only ~half the time; on gpt-4o it fires reliably, produces
the full graded outcome spectrum, and its narration tracks the mechanics. The extractor and
embedder are high-volume and fine on a cheap model. So the recommended split is a **strong
planner/narrator + cheap extractor/embedder**, e.g. `uro play <c> --provider openai
--role-model planner=gpt-4o --role-model narrator=gpt-4o` (extractor stays gpt-4o-mini). This is
exactly what `scripts/postpoc_validate.sh` now does.

## Capability probes

Per owner feedback: when a world declares requirements (e.g. mature content enabled), there must be a way to **test whether the bound models can actually deliver**. `uro world probe <world>` runs a suite against every bound role and produces a report (**PoC: printed to stdout with raw transcripts; persisted, timestamped report storage is deferred**). The shipped suite is two of the probes below — the hard `structured_output` gate and `content_rating`; the rest extend the same ask→judge→attach-transcript pattern:

| Probe | Checks | Gate for |
|---|---|---|
| `structured_output` | Schema compliance rate over N tries | planner, extractor (hard requirement) |
| `context_window` | Declared vs. usable effective context | all roles |
| `content_rating` | Generates test prompts for each **enabled** category (from the canonical dimension set: violence / horror / sexual_content / profanity, `09`) at the world's `rating` intensity, scores refusal vs. compliance. `disabled` categories are not probed — the engine doesn't enforce suppression (that's moderation, D-5) | narrator, dialogue |
| `instruction_following` | Style-pack adherence (tone, tense, POV) | narrator, dialogue |
| `consistency` | Given seeded facts, does output contradict them | narrator |
| `latency` | p50/p95 per role | informational |

Result semantics: **compatibility report, not enforcement** — a world declaring NSFW bound to a refusing model gets a loud warning (and platforms can choose to hard-fail); the engine itself still runs. This is content-agnosticism made practical: policy stays with providers and platforms, *testability* lives in the engine.

Scoring (D-24): probe outputs are graded by the **judge role** against per-probe rubrics, with refusal-pattern heuristics as fallback; every report attaches the raw transcripts so a human can overrule the machine. Bind a different model as judge than the one under test where possible (self-judging circularity: OQ-9). Probes are a **best-effort compatibility signal, not certification** — treat scores as smoke detection, not a guarantee.

## Memory & retrieval (current design; more brainstorming expected — OQ-2, OQ-3)

Context assembly for any beat combines:

1. **Structured recall (primary):** direct state queries. **As built (`pipeline/recall.py`):** on-stage actors (name/alias-matched, dead excluded), their beliefs, and claims linked to those entities — assembled deterministically. *Place-state and active-thread recall are the design intent but NOT yet assembled into the prompt* (only actors/claims/beliefs + semantic memories are). The knowledge graph answers "what is true" *precisely*; this always beats semantic search when refs exist.
2. **Semantic recall (secondary):** pgvector search over embedded memories (chronicle entries, T3 actor journals, past scene synopses) for "what is *relevant*" — thematic echoes, old promises, foreshadowing. (The `embedder` role is also *designed* to maintain a separate corpus of entity name/alias strings for entity resolution — see `07` `entity_index` and `13`; same role, different index — **not built yet**.)
3. **Recency window:** last few beats verbatim.
4. **Compression:** summarizer role periodically folds old beats into synopses (per campaign) and journal entries (per T3 actor); originals stay in the log — compression affects *recall*, never *truth*.

Known-hard problem, flagged by the report and the owner alike: *knowing what to recall*. MVP heuristic: entity-triggered structured recall + top-k semantic + recency, then iterate with play data.
