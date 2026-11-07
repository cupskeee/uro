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
│   ├── planner.system.j2   #   exactly (uro_core/prompts/*.j2), else it is not applied)
│   ├── extractor.system.j2
│   └── pacing.j2           #   the pacing director's line (D-96), rendered with `stakes`
└── assets/                 # opaque blobs for platforms (maps, portraits) — engine
                            #   stores/serves refs, never interprets them
```

### `world.toml`

```toml
[world]
name = "Ashfall"
tone = ["grim", "low-magic", "political"]
description = "A coast of salt and old debts, where the dukes still owe the dead."   # D-104: the pitch a platform lists (play's prompts never see it; backfill reads it)
starting_gear = ["a traveler's knife"]   # D-97: what a fresh PC carries here; none if omitted

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

[calendar]                   # meant to derive years & seasons from the day counter (D-22): parsed and
                             # validated, not yet consumed or carried in WorldGenesis (D-93)
days_per_year = 360
seasons = ["thaw", "highsun", "harvest", "longdark"]
epoch_label = "After the Sundering"   # single epoch for date rendering. Named narrative eras
                                      # (e.g. history.seed_era below) are EVENT-driven, not arithmetic —
                                      # a History-layer concept, not derived from the day counter.

[clock]                      # D-80: what moves the world's day besides an explicit time-skip
from_intent = true           # an intent that passes whole days moves it (D-77); false = the game
                             # moves it itself (Engine.pass_time, or the REST/CLI time-skip with
                             # downtime off). The day stays world state either way.

[pacing]                     # D-95/D-96: OFF by default. The engine always records which plots
quiet_after = 25             #   came up (and reports quiet ones, `/pacing`); setting quiet_after
every = 10                   #   opts in: a live thread untouched that many beats is named to the
                             #   narrator, at most once every `every` beats (default 10). The
                             #   line is the pack's prompts/pacing.j2. worlds/ashfall opts in.

[ruleset]
id = "uro-basic"             # resolved via the ruleset registry (06, D-30); "uro-pbta" for the
version = ">=0.1"            #   alien PbtA build. A campaign started on the world PINS this, so
[ruleset.config]             #   play/fork rebinds the same ruleset. Example: worlds/emberfell
                             #   (id="uro-pbta") plays under the 2d6 system, worlds/ashfall d20,
                             #   worlds/brinehold (id="uro-pool") the d6 dice pool (D-55).
                             # The game's settings for its ruleset (D-62), e.g. `push_strain = 2`.
                             #   `uro ruleset list` / GET /rulesets show each ruleset's settings
                             #   and defaults; an unknown or invalid key fails validate and pack import.
                             #   (A world BUNDLE import keeps the world even if its ruleset
                             #   isn't installed here: it plays once the ruleset is.)
                             #   Stored on the world's WorldGenesis, so every campaign and fork of
                             #   it plays the same rules.

[history]
seed_era = "aftermath-of-empire"   # hint to the History service
simulate_years = 200

[history.vocabulary]         # D-97: the words `uro world seed` builds the past from. The engine
groups = ["Corvane", "Ashmoor"]  # ships none (no kings, no houses): a pack without a vocabulary
group_name = "House {name}"  # can't be seeded. {name}: one of `groups`.
group_description = "A dynasty risen in {era}."   # {era}: "the age of <seed_era>"
titles = ["King", "Duke"]    # optional: a leader is "<title> <leader>", or just "<leader>"
leaders = ["Aldric", "Mira"]
leader_role = "ruler"
calamities = ["a great plague", "a border war"]   # what marked the era
# optional, in the pack's language (the English defaults): era_name = "the age of {era}",
# era_unnamed = "its past", era_claim = "{era_name} was marked by {calamity}."

[llm.roles]                  # SUGGESTIONS; deployment config overrides
narrator = "anthropic:claude-sonnet-5"
```

## Authoring entities — who knows whom (D-76)

An actor may list the actors it knows: `knows: ["a:hesk"]`. Acquaintance is mutual — import
commits a `knows` edge each way — and it is the only relation a rumor travels along
(`engines/actor.propagate_belief`: the witnesses hold it at 0.9, each retelling keeps 0.55 of
that, and it stops below 0.2, so two hops out a rumor is "has heard a rumor"). A `knows` ref that
names no actor in the pack, or the actor itself, is refused at parse. Rumors spread at the
moments a world defines them (a world rule's or an agenda's `spread_belief`, a Chronicler
outcome's witnesses). The extractor never adds `knows` edges; only the pack does (its seeds, or a
world rule's or agenda's `add_edge`). `worlds/ashfall` carries a rumor from the docks two hops
inland, into the next region, this way (its `agendas.yaml`). A spread seeds its witnesses and
re-tells the whole chain each time it runs, overwriting what those actors held, so make a one-off
spread one-off (Ashfall gates its own on a counter).

Rule files: `rules.yaml` holds `rules`, `agendas.yaml` holds `agendas`, each with
`rules_api_version`. Any other top-level key is refused at parse (an `agendas:` list written
into `rules.yaml` used to be accepted and never run). One file may leave the version to the other;
two that both declare one must agree.

## Authoring entities — colloquial aliases (best practice)

`entities/actors.yaml` items carry an optional `aliases: [...]`. **Give every named NPC the short
names players (and the narrator) will actually use** — an authored `name: "Cass Holloway"` should
list `aliases: ["Cass"]`. Recall matches on-stage actors by name *or* alias, and entity resolution
(`find_actor_by_name`, the extractor, Chronicler ingestion) folds a reference to an existing actor
by name/alias. Without the alias, a "Cass" reference doesn't match "Cass Holloway" (canonical
matching is not substring/first-name) and the extractor mints a *duplicate* actor — the OQ-3
fragmentation the first PbtA live run hit (docs/16). Aliases are the sanctioned fix; automatic
first-name/partial matching is deliberately NOT done (false-merge risk, OQ-3).

## Prompt template packs

Owner decision: prompt templates live **at the world level**, distributable with the pack, customizable by any engine user. Mechanism:

- The engine ships **default templates** for every pipeline stage (`uro_core/prompts/narrator.system.j2`, `planner.system.j2`, `extractor.system.j2`), and for the pacing director's line (`pacing.j2`, D-96: injected `stakes`, a thread's stakes without its final period; a world that opts in can reword or translate it).
- A pack's `prompts/` overrides any subset **by matching that exact filename**; everything else falls through to defaults (`PromptEnv`, `pipeline/prompts.py`). A file whose name doesn't match a default template name is simply never rendered.
- A pack can instead **add to** a template (D-106): `prompts/<template>.append.j2` (`narrator.system.append.j2`) renders after the template, whether the engine's default or the pack's own override, with the same context, separated by a blank line. A world that only adds a premise or a style note this way stays on the engine's current default as it changes, instead of freezing a copy. An addendum to a template the engine doesn't ship is refused at parse (it would never render). A narrator addendum counts toward the Tone dimension like a narrator override.
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

`uro world validate` stops at the report. `uro world create` (import) goes further: it commits `WorldGenesis`, then — in the **same import commit, emitter `S`** — emits `PlaceCreated`/`FactionCreated`/`ActorCreated` for every seed entity, `EdgeAdded` for every authored/cross-linked relation, `ClaimRecorded` for authored claims, and `ThreadCreated` for every conflict seed (`12` whitelists `S` on all of these). With `uro world create --backfill`, AI-filled seeds ride the same path and their `ThreadCreated` carries `provenance=ai_backfill` — so the machine's inventions are reviewable committed state, queryable via `proj_threads`. So authored geography, actors, factions, relations, and threads exist as timeline state *before* any History seeding runs. These are seed-independent (they're what the author wrote); History seeding (`H`, run by `uro world seed`) then layers a seed-dependent past on top (power groups, their leaders and conflicts, in the pack's own `[history.vocabulary]`, D-97) — which is why identical geography survives across different seeds (`03`, Phase 4 acceptance). A world that sets no `history.simulate_years` still has its authored entities, because they land at create, not seed.

The **sufficiency check** scores the assembled world against a coverage rubric — the dimensions the pipeline actually needs at runtime:

| Dimension | Question | Minimum for "runnable" |
|---|---|---|
| Geography | Somewhere to be? | ≥1 place, of any kind (D-97: a world may be one ship, one house, one station; `region`/`settlement`/`site` describe a place, they aren't a required shape) |
| Population | Someone to meet? | ≥3 seeded actors, or `generate_population = true` while the instance lets play create actors (D-81: the people are made in play — the narrator names them, the extractor records them; nothing is generated up front) |
| Power | Who runs things? | ≥1 faction, or an actor of tier 2+ (D-97: no role words — the engine doesn't know what a ruler is called in a game) |
| Conflict seeds | Anything to play about? | ≥1 tension/thread hook |
| Tone | How should it sound? | tone tags or style template present |
| History | Any past to reference? | overview lore ≥ threshold, or `history.simulate_years` set |

Output is a graded report (`runnable | thin | insufficient`) with specific gaps ("no conflict seeds found — campaigns will open aimless"). The shipped policy: `uro world create` refuses only `insufficient` (nothing to play) and imports `thin` with the grade surfaced. The **assisted** path is opt-in — `uro world backfill` previews AI-generated seeds (stdout only, rewrites nothing), and `uro world create --backfill` commits them as `ThreadCreated` events tagged `provenance=ai_backfill` (queryable via `proj_threads`) so authors can review and platforms can display what the machine invented. Backfill fills **every** rubric dimension (D-108), one structured `worldsmith` call per gap, in an order where each builds on the last: places (a region, a settlement in it, a site in that), a faction, people up to three (placed and affiliated by name), a conflict, tone tags, and a past (facts committed as claims with `origin=ai_backfill`, which reach play, plus their overview in `lore/ai_backfill/history.md`, which the grade reads). Places, people, factions and threads carry `provenance=ai_backfill` on their committed events and rows; the manifest's `backfilled` lists the dimensions filled (the record for tone, which has no entity to tag), and the world keeps it (D-104). A reply that can't be used fills nothing and the gap stays reported. `create --backfill` tries an `insufficient` pack too, and refuses it only if it stays insufficient. Names are invented only where the gap is the absence of named things. Silent invention of an author's world is never the default. *(A hard `--strict` refuse-below-runnable flag is not shipped.)*

`uro world validate ./pack/` runs everything except import; validation + probes + dry-run together are the whole creator loop this engine offers — file-first, UI-later-by-someone-else.
