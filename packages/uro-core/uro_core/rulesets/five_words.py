"""The threat scale the d20, 2d6 and dice-pool built-ins share (D-116): five words for how
dangerous someone would be in a fight. It lives beside those rulesets, not in the port: a game
whose people are sized up some other way declares its own `ThreatScale`, or none."""

from __future__ import annotations

from uro_core.rulesets.base import ThreatScale

FIVE_WORDS = ThreatScale(
    words=("harmless", "weak", "average", "strong", "deadly"),
    measures="how dangerous they would be in a fight",
    examples='"a hulking veteran in plate" → strong; "a frightened child" → harmless',
)
