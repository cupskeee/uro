# 09 — World Definition Format

The engine's answer to "creator tools": **a well-specified file format, not a UI** (decision D-6 — a world-builder UI is a platform concern for an immature engine). A world pack is a directory (or `.uwp` archive) any platform can generate, edit, and distribute; the engine validates, imports, and runs it.

## Pack layout

```
ashfall/
├── world.toml              # manifest (below)
├── lore/                   # freeform markdown — the author's voice
│   ├── overview.md         #   history, cosmology, cultures, conflicts...
│   └── regions/*.md
├── entities/               # structured seeds (YAML): places, actors, factions
│   ├── places.yaml         #   (religions = factions with kind=religion, see 02),
│   ├── factions.yaml       #   claims — as much or little as the author
│   └── actors.yaml         #   wants to pin down explicitly
├── prompts/                # prompt template pack (optional, overrides defaults BY FILENAME —
│   ├── narrator.system.j2  #   the file's name must match the engine's default template name
│   ├── planner.system.j2   #   exactly (uro_core/prompts/*.system.j2), else it is not applied)
│   └── extractor.system.j2
└── assets/                 # opaque blobs for platforms (maps, portraits) — engine
                            #   stores/serves refs, never interprets them
```

### `world.toml`

```toml
[world]
name = "Ashfall"
tone = ["grim", "low-magic", "political"]

[content]                    # per-world, NOT per-user (owner decision)
rating   = "mature"          # intensity CEILING: none | mild | mature | explicit
enabled  = ["violence", "horror"]        # content CATEGORIES in play (the dimension vocabulary)
disabled = ["sexual_content"]            # categories the world excludes
# Two axes: `rating` is how intense, enabled/disabled is which categories. A world runs at
# `rating` intensity across its `enabled` categories. Recognized dimension vocabulary:
# violence, horror, sexual_content, profanity (canonical set shared with the probe, `04`).
# The content_rating probe (`04`) tests ENABLED categories at the `rating` tier and warns on
# refusal; the engine does NOT probe or enforce `disabled` (that would be moderation — D-5);
# `disabled` is a declaration consumed by prompt packs and consuming platforms.

[calendar]                   # derives years & seasons from the day counter (D-22)
days_per_year = 360
seasons = ["thaw", "highsun", "harvest", "longdark"]
epoch_label = "After the Sundering"   # single epoch for date rendering. Named narrative eras
                                      # (e.g. history.seed_era below) are EVENT-driven, not arithmetic —
                                      # a History-layer concept, not derived from the day counter.

[ruleset]
id = "uro-basic"             # or any installed plugin
version = ">=0.1"
[ruleset.config]             # ruleset-specific knobs

[history]
seed_era = "aftermath-of-empire"   # hint to the History service
simulate_years = 200

[llm.roles]                  # SUGGESTIONS; deployment config overrides
narrator = "anthropic:claude-sonnet-5"
```

## Prompt template packs

Owner decision: prompt templates live **at the world level**, distributable with the pack, customizable by any engine user. Mechanism:

- The engine ships **default templates** for every pipeline stage (`uro_core/prompts/narrator.system.j2`, `planner.system.j2`, `extractor.system.j2`).
- A pack's `prompts/` overrides any subset **by matching that exact filename**; everything else falls through to defaults (`PromptEnv`, `pipeline/prompts.py`). A file whose name doesn't match a default template name is simply never rendered.
- Templates are Jinja2 (`StrictUndefined` — an override that references a variable the stage doesn't inject fails loudly) with a documented context contract per stage (e.g. the narrator injects `style` = the world's tone). A `TEMPLATE_API_VERSION` constant exists as the intended version-pin anchor, but the manifest carries no version field yet and nothing checks it — **the pin is reserved, not yet enforced.**
- This is deliberately the *only* creator scripting surface for now — no module/plugin scripting in world packs (deferred, D-6); world-pack authors customize behavior through prompts and seeds, not code.

## Import pipeline and the sufficiency check

Owner requirement: guard the mechanism — *if the lore is too minimal to run a world, the author must be told.*

```
parse & schema-validate  →  entity extraction  →  cross-linking  →  SUFFICIENCY CHECK  →  report
                            (LLM-assisted:        (resolve refs
                             pull implied          between lore
                             entities out of       and seeds)
                             lore/*.md)
```

`uro world validate` stops at the report. `uro world create` (import) goes further: it commits `WorldGenesis`, then — in the **same import commit, emitter `S`** — emits `PlaceCreated`/`FactionCreated`/`ActorCreated` for every seed entity, `EdgeAdded` for every authored/cross-linked relation, `ClaimRecorded` for authored claims, and `ThreadCreated` for every conflict seed (`12` whitelists `S` on all of these). With `uro world create --backfill`, AI-filled seeds ride the same path and their `ThreadCreated` carries `provenance=ai_backfill` — so the machine's inventions are reviewable committed state, queryable via `proj_threads`. So authored geography, actors, factions, relations, and threads exist as timeline state *before* any History seeding runs. These are seed-independent (they're what the author wrote); History seeding (`H`, run by `uro world seed`) then layers seed-dependent dynasties/wars on top — which is why identical geography survives across different seeds (`03`, Phase 4 acceptance). A world that sets no `history.simulate_years` still has its authored entities, because they land at create, not seed.

The **sufficiency check** scores the assembled world against a coverage rubric — the dimensions the pipeline actually needs at runtime:

| Dimension | Question | Minimum for "runnable" |
|---|---|---|
| Geography | Somewhere to be? | ≥1 region, ≥1 settlement, ≥1 site |
| Population | Someone to meet? | ≥3 seeded actors or explicit "generate freely" flag |
| Power | Who runs things? | ≥1 faction or ruler |
| Conflict seeds | Anything to play about? | ≥1 tension/thread hook |
| Tone | How should it sound? | tone tags or style template present |
| History | Any past to reference? | overview lore ≥ threshold, or `history.simulate_years` set |

Output is a graded report (`runnable | thin | insufficient`) with specific gaps ("no conflict seeds found — campaigns will open aimless"). The shipped policy: `uro world create` refuses only `insufficient` (nothing to play) and imports `thin` with the grade surfaced. The **assisted** path is opt-in — `uro world backfill` previews AI-generated seeds (stdout only, rewrites nothing), and `uro world create --backfill` commits them as `ThreadCreated` events tagged `provenance=ai_backfill` (queryable via `proj_threads`) so authors can review and platforms can display what the machine invented. Backfill currently fills the **conflict** dimension; the other rubric dimensions extend the same ask→generate→tag pattern (not yet wired). Silent invention of an author's world is never the default. *(A hard `--strict` refuse-below-runnable flag is not shipped.)*

`uro world validate ./pack/` runs everything except import; validation + probes + dry-run together are the whole creator loop this engine offers — file-first, UI-later-by-someone-else.
