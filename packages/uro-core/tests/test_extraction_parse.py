"""Extractor response parsing + the whitelist-by-schema property (no DB)."""

import logging

import pytest
from uro_core.pipeline.extraction import (
    _is_role_noun,
    build_extractor_messages,
    canonical_name,
    parse_extraction,
)
from uro_core.pipeline.recall import RecallBundle
from uro_core.timeline.models import FactionView, ThreadView


def test_canonical_name_folds_articles_case_and_whitespace() -> None:
    # the observed live split: "the woman" / "woman" / "The Woman" must canonicalize to one entity
    assert canonical_name("the woman") == "woman"
    assert canonical_name("The  Woman ") == "woman"
    assert canonical_name("A Guard") == "guard"
    assert canonical_name("the Duke") == "duke"
    # ...but partial words and article-less names are untouched (no over-merging)
    assert canonical_name("another") == "another"  # not "other"
    assert canonical_name("theater") == "theater"
    assert canonical_name("Marla") == "marla"


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
    # extractor structurally cannot attribute a claim to player text (trust model). The bad
    # claim is dropped on its own (per-item salvage, D-51) — it never reaches the gauntlet.
    ex = parse_extraction(
        '{"claims":[{"statement":"x","provenance":"player"},{"statement":"The well is dry."}]}'
    )
    assert ex is not None
    assert [c.statement for c in ex.claims] == ["The well is dry."]


def test_null_optional_fields_do_not_drop_the_beat() -> None:
    # D-51, the live failure mode: a model answers "(optional)" keys with null. Validating the
    # whole object at once discarded EVERY actor/place/claim in the beat over one null.
    ex = parse_extraction(
        '{"actors":[{"name":"Mara","role":null,"member_of":null,"located_in":null}],'
        '"places":[{"name":"Harrowmere","description":null,"parent":null}],'
        '"claims":[{"statement":"Mara fishes the lagoon.","about":["Mara",null],'
        '"speaker":null,"contradicts":null,"confidence":null}]}'
    )
    assert ex is not None
    assert ex.actors[0].name == "Mara" and ex.actors[0].member_of == ""
    assert ex.places[0].name == "Harrowmere" and ex.places[0].parent == ""
    assert ex.claims[0].about == ["Mara"] and ex.claims[0].contradicts == []


def test_a_malformed_item_is_dropped_alone() -> None:
    ex = parse_extraction(
        '{"actors":[{"role":"guard"},{"name":"Nessa"}],"threads":[{}, {"stakes":"A war looms."}],'
        '"factions":"not a list","claims":[{"statement":"Nessa left an inland village."}]}'
    )
    assert ex is not None
    assert [a.name for a in ex.actors] == ["Nessa"]  # the nameless actor is dropped
    assert [t.stakes for t in ex.threads] == ["A war looms."]
    assert ex.factions == []  # a non-list is ignored, not fatal
    assert len(ex.claims) == 1


def test_non_object_json_returns_none() -> None:
    assert parse_extraction('["Mara", "Tavi"]') is None  # no object → re-ask, as before


def test_extractor_sees_known_factions_and_threads() -> None:
    # D-51: without the known threads in view, the extractor re-proposed the same plot every beat.
    recall = RecallBundle(
        recent_beats=[],
        actors=[],
        claims=[],
        beliefs=[],
        factions=[FactionView(faction_id="f:1", name="Iron Order", kind="faction", description="")],
        active_threads=[
            ThreadView(thread_id="t:1", stakes="A war looms.", state="active", provenance="author")
        ],
    )
    user = build_extractor_messages(recall, "Ser Alden rides out.")[1].content
    assert "KNOWN FACTIONS:\n- Iron Order [f:1]" in user
    assert "KNOWN THREADS:\n- A war looms." in user
    assert "never null" in user


def test_unknown_event_kinds_are_not_representable() -> None:
    # The schema has only actors and claims — no way to express damage/death/terrain.
    ex = parse_extraction('{"actors":[],"claims":[],"damage":[{"actor":"x","amount":5}]}')
    assert ex is not None  # extra keys ignored
    assert ex.actors == [] and ex.claims == []  # nothing mechanical crossed the boundary


def test_parse_survives_pathological_nesting() -> None:
    # Deeply nested input can raise RecursionError in json.loads; parse must return None,
    # never propagate and abort the beat (prose is never lost — review Phase-1.2).
    assert parse_extraction("[" * 2000 + "]" * 2000) is None


def test_role_noun_detection() -> None:
    # D-51: a description is all-lowercase once its article is stripped; a name is capitalized.
    for role in ("the guard", "tallest hooded stranger", "a barkeep", "An old patron"):
        assert _is_role_noun(role), role
    for name in ("the Duke", "Ser Aldren Voss", "Mara", "de la Cruz"):
        assert not _is_role_noun(name), name
    assert not _is_role_noun("衛兵")  # no letter case → can't judge → never refused


def test_a_dropped_item_is_logged_with_its_content(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="uro_core.pipeline.extraction"):
        parse_extraction('{"actors":[{"role":"the nameless one"}]}')
    assert "the nameless one" in caplog.text  # what was dropped, not just a count
