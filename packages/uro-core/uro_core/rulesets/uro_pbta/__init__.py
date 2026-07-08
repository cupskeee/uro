"""Built-in "Uro PbtA" ruleset — a deliberately ALIEN, non-d20 system (OQ-13, D-30).

Concrete impl — the core ring imports the ruleset PORT, never this (import-linter, docs/14).
The port-generality probe: a PbtA-style 2d6 system whose shape shares nothing with d20 — no
ability scores, no hp/ac, no initiative, no binary success. If the generic port needed no d20
word to host BOTH uro_basic and this, the port is genuinely game-agnostic.
"""

from uro_core.rulesets.uro_pbta.ruleset import Sheet, UroPbtA

__all__ = ["Sheet", "UroPbtA"]
