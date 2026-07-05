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
├── prompts/                # prompt template pack (optional, overrides defaults)
│   ├── narrator.style.j2
│   ├── dialogue.style.j2
│   └── planner.hints.j2
└── assets/                 # opaque blobs for platforms (maps, portraits) — engine
                            #   stores/serves refs, never interprets them
```

### `world.toml`

```toml
[world]
name = "Ashfall"
tone = ["grim", "low-magic", "political"]

[content]                    # per-world, NOT per-user (owner decision)
rating   = "mature"          # none | mild | mature | explicit
enabled  = ["violence", "horror"]        # dimensions the world expects
disabled = ["sexual_content"]            # probes test exactly these declarations

[calendar]                   # derives years/seasons/eras from the day counter (D-22)
days_per_year = 360
seasons = ["thaw", "highsun", "harvest", "longdark"]
epoch_label = "After the Sundering"

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

- The engine ships **default templates** for every pipeline stage/role (part of `uro-core`, versioned).
- A pack's `prompts/` overrides any subset by filename; everything else falls through to defaults.
- Templates are Jinja2 with a documented, stable context contract per stage (the variables `05-generation-pipeline.md` stages inject). Template pack version pins against a template-API version so engine upgrades fail loudly, not weirdly.
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

The **sufficiency check** scores the assembled world against a coverage rubric — the dimensions the pipeline actually needs at runtime:

| Dimension | Question | Minimum for "runnable" |
|---|---|---|
| Geography | Somewhere to be? | ≥1 region, ≥1 settlement, ≥1 site |
| Population | Someone to meet? | ≥3 seeded actors or explicit "generate freely" flag |
| Power | Who runs things? | ≥1 faction or ruler |
| Conflict seeds | Anything to play about? | ≥1 tension/thread hook |
| Tone | How should it sound? | tone tags or style template present |
| History | Any past to reference? | overview lore ≥ threshold, or `history.simulate_years` set |

Output is a graded report (`runnable | thin | insufficient`) with specific gaps ("no conflict seeds found — campaigns will open aimless"). Two policies: **strict** (refuse below `runnable`) and **assisted** — the History/World services offer an **AI backfill pass** filling declared gaps, with every generated element event-tagged `provenance=ai_backfill` so authors can review and platforms can display what the machine invented. Backfill is opt-in; silent invention of an author's world is never the default.

`uro world validate ./pack/` runs everything except import; validation + probes + dry-run together are the whole creator loop this engine offers — file-first, UI-later-by-someone-else.
