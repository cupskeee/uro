"""Extractor response parsing + the whitelist-by-schema property (no DB)."""

from uro_core.pipeline.extraction import parse_extraction


def test_parses_plain_json() -> None:
    ex = parse_extraction('{"actors":[{"name":"Mera","role":"innkeeper"}],"claims":[]}')
    assert ex is not None
    assert ex.actors[0].name == "Mera" and ex.actors[0].role == "innkeeper"


def test_parses_json_wrapped_in_fences() -> None:
    ex = parse_extraction(
        '```json\n{"actors":[],"claims":[{"statement":"The door is locked."}]}\n```'
    )
    assert ex is not None and ex.claims[0].statement == "The door is locked."


def test_garbage_returns_none() -> None:
    assert parse_extraction("the model refused to answer") is None


def test_player_provenance_is_not_representable() -> None:
    # provenance is Literal[narrator, dialogue]; 'player' fails validation, so the
    # extractor structurally cannot attribute a claim to player text (trust model).
    assert parse_extraction('{"claims":[{"statement":"x","provenance":"player"}]}') is None


def test_unknown_event_kinds_are_not_representable() -> None:
    # The schema has only actors and claims — no way to express damage/death/terrain.
    ex = parse_extraction('{"actors":[],"claims":[],"damage":[{"actor":"x","amount":5}]}')
    assert ex is not None  # extra keys ignored
    assert ex.actors == [] and ex.claims == []  # nothing mechanical crossed the boundary


def test_parse_survives_pathological_nesting() -> None:
    # Deeply nested input can raise RecursionError in json.loads; parse must return None,
    # never propagate and abort the beat (prose is never lost — review Phase-1.2).
    assert parse_extraction("[" * 2000 + "]" * 2000) is None
