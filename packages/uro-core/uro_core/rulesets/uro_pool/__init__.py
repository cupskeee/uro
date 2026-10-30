"""Built-in "Uro Pool" ruleset — an original d6 dice-pool system, the third built-in (D-55).

Concrete impl — the core ring imports the ruleset PORT, never this (import-linter, docs/14).
It keeps the port honest on axes neither earlier built-in touches: target-number dice pools,
push-your-luck strain, capability-degrading wounds, and SIMULTANEOUS exchanges (actions that
produce no effect until the round closes).
"""

from uro_core.rulesets.uro_pool.ruleset import Sheet, UroPool

__all__ = ["Sheet", "UroPool"]
