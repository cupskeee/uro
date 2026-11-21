"""Built-in "Uro Composable" ruleset — a game that is entirely its settings (D-114).

Concrete impl — the core ring imports the ruleset PORT, never this (import-linter, docs/14). It
names no game word: a definition (ratings on a step scale, tracks, tags, modifiers, actions,
choices) and a resolver (d20, 2d6 or a pool) arrive as settings, validated like any ruleset's.
"""

from uro_core.rulesets.uro_composable.ruleset import ComposableConfig, Sheet, UroComposable

__all__ = ["ComposableConfig", "Sheet", "UroComposable"]
