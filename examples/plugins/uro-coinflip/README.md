# uro-coinflip — an example ruleset plugin

A complete, deliberately tiny Uro ruleset distributed as its own package: every check and every
blow is a coin flip. It exists to show what a plugin needs (docs/06, D-65).

```sh
pip install -e examples/plugins/uro-coinflip   # into the same environment as Uro
uro ruleset list                               # coinflip@1, with its settings
```

A world plays it by declaring it in `world.toml`:

```toml
[ruleset]
id = "coinflip"

[ruleset.config]
coins = 2        # flip two coins; one heads succeeds
```

What a plugin must provide:
- **An entry point** in the `uro.rulesets` group whose name is the ruleset's `id`, pointing at a
  factory that takes the world's settings (`__init__(config)` on the class works).
- **`api_version = RULESET_API_VERSION`**, the port version it implements. The registry refuses a
  plugin built for another version, so it fails at load rather than mid-beat.
- **Every port method** in `uro_core.rulesets.base.Ruleset`: the character model, free-roam
  checks, the encounter loop, and the D-63 hooks (`conclude_encounter`, `downtime`).
- **Settings validation:** refuse an unknown key (`extra="forbid"`), so a typo in a pack fails
  loudly.

A plugin that fails any of these is listed by `uro ruleset list` with the reason and can't be
played. It can never replace a built-in.
