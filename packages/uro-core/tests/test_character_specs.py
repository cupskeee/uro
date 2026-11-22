"""Character specs on the ruleset port (D-112): a ruleset refuses a spec it can't build with
located problems, reads the engine's one neutral hint (`threat`), publishes the schema of what it
takes, and says which part of a check a player may see. All optional on the port."""

from typing import Any

import pytest
from uro_core.rulesets import registry
from uro_core.rulesets.base import (
    CharSpec,
    CharSpecError,
    CheckRequest,
    ThreatScale,
    build_character,
    spec_schema_of,
    threat_problem,
    threat_scale,
)
from uro_core.rulesets.five_words import FIVE_WORDS
from uro_core.rulesets.rng import Rng

BUILT_INS = ("uro-basic", "uro-pbta", "uro-pool")


@pytest.mark.parametrize("rid", BUILT_INS)
def test_an_empty_spec_always_builds(rid: str) -> None:
    ruleset = registry.resolve(rid)
    assert build_character(ruleset, CharSpec(), Rng(0)) == ruleset.new_character(CharSpec(), Rng(0))


@pytest.mark.parametrize(
    ("rid", "data", "loc"),
    [
        ("uro-basic", {"abilities": {"LUCK": 3}}, ["abilities", "LUCK"]),
        ("uro-basic", {"level": 9}, ["level"]),
        ("uro-basic", {"mana": 1}, ["mana"]),
        ("uro-pbta", {"stats": {"forceful": 7}}, ["stats", "forceful"]),
        ("uro-pbta", {"playbook": "x"}, ["playbook"]),
        ("uro-pool", {"traits": {"might": 0}}, ["traits", "might"]),
        ("uro-pool", {"traits": {"might": 9}}, ["traits", "might"]),
    ],
)
def test_a_bad_spec_is_refused_with_located_problems(
    rid: str, data: dict[str, Any], loc: list[str]
) -> None:
    with pytest.raises(CharSpecError) as err:
        build_character(registry.resolve(rid), CharSpec(data=data), Rng(0))
    assert [p.loc for p in err.value.problems] == [loc]
    assert all(p.message for p in err.value.problems)


def test_the_caps_come_from_the_worlds_settings() -> None:
    capped = registry.resolve("uro-pool", "", {"trait_cap": 2})
    with pytest.raises(CharSpecError, match="at most 2"):
        build_character(capped, CharSpec(data={"traits": {"might": 3}}), Rng(0))
    # a hint is capped rather than refused: it's the engine's word, not the player's build
    assert max(build_character(capped, CharSpec(threat="deadly"), Rng(0))["traits"].values()) == 2


def test_a_word_outside_the_rulesets_scale_is_no_hint_at_play_time() -> None:
    """D-116: the words are the ruleset's. An actor keeps the word it got under one game, and a
    campaign may play the world by another, so a foreign word builds the default, never fails."""
    ruleset = registry.resolve("uro-basic")
    assert build_character(ruleset, CharSpec(threat="boss"), Rng(0)) == build_character(
        ruleset, CharSpec(), Rng(0)
    )
    assert threat_problem(ruleset, "boss") == "threat 'boss': one of " + ", ".join(FIVE_WORDS.words)
    assert threat_problem(ruleset, "strong") is None and threat_problem(ruleset, "") is None


def test_a_ruleset_with_no_scale_reads_no_threat_word() -> None:
    class Plain:
        id = "plain"

        def new_character(self, spec: CharSpec, rng: Rng) -> dict[str, Any]:
            return {"hint": spec.threat}

    assert threat_scale(Plain()) is None
    assert build_character(Plain(), CharSpec(threat="strong"), Rng(0)) == {"hint": ""}
    assert threat_problem(Plain(), "strong") == "threat 'strong': plain reads no threat word"


def test_a_definition_names_its_own_scale() -> None:
    """A composable game's foe tiers carry its own words, in order; the five standard words keep
    their fight reading, and a game with no foe tiers sizes nobody up."""
    own = registry.resolve(
        "uro-composable",
        config={
            "definition": {
                "ratings": [{"key": "poise"}],
                "foes": [
                    {"key": "pest", "steps": 0, "threat": ["nuisance"]},
                    {"key": "rival", "steps": 1, "threat": ["rival", "menace"]},
                ],
                "threat_measures": "how hard they are to out-argue",
                "threat_examples": '"a bored clerk" → nuisance',
            },
            "resolver": {"kind": "2d6"},
        },
    )
    scale = threat_scale(own)
    assert scale is not None and scale.words == ("nuisance", "rival", "menace")
    assert scale.measures == "how hard they are to out-argue"
    rival = build_character(own, CharSpec(threat="menace"), Rng(0))
    plain = build_character(own, CharSpec(threat="deadly"), Rng(0))  # not this game's word
    assert rival["ratings"]["poise"] == 1 and plain == build_character(own, CharSpec(), Rng(0))
    assert threat_scale(registry.resolve("uro-composable", config={"definition": {}})) is None
    # tiers picked only by standard words keep the whole five, so `average` is still a word
    usual = registry.resolve(
        "uro-composable",
        config={
            "definition": {
                "ratings": [{"key": "poise"}],
                "foes": [{"key": "brute", "steps": 1, "threat": ["strong"]}],
            },
            "resolver": {"kind": "2d6"},
        },
    )
    assert threat_scale(usual) == FIVE_WORDS
    assert threat_problem(usual, "average") is None


def test_a_scales_words_are_plain() -> None:
    for words in (("Minor",), (" minor",), ("",), ("minor", "minor")):
        with pytest.raises(ValueError, match="threat words"):
            ThreatScale(words=words, measures="x")


@pytest.mark.parametrize("rid", BUILT_INS)
def test_threat_orders_the_built_ins_and_the_spec_wins(rid: str) -> None:
    ruleset = registry.resolve(rid)

    def power(sheet: dict[str, Any]) -> int:
        for key in ("abilities", "stats", "traits"):
            if key in sheet:
                return sum(sheet[key].values()) + sheet.get("level", 0) * 10
        raise AssertionError(sheet)

    ranked = [power(build_character(ruleset, CharSpec(threat=t), Rng(0))) for t in FIVE_WORDS.words]
    assert ranked == sorted(ranked) and ranked[0] < ranked[-1]
    own = {
        "uro-basic": {"level": 1},
        "uro-pbta": {"stats": {"forceful": 0}},
        "uro-pool": {"traits": {"might": 1}},
    }[rid]
    sheet = build_character(ruleset, CharSpec(data=own, threat="deadly"), Rng(0))
    key, value = next(iter(own.items()))
    got = sheet[key] if not isinstance(value, dict) else {k: sheet[key][k] for k in value}
    assert got == value  # the spec's own value beats what the hint implies


@pytest.mark.parametrize("rid", BUILT_INS)
def test_the_built_ins_publish_their_spec_schema(rid: str) -> None:
    schema = spec_schema_of(registry.resolve(rid))
    assert schema["type"] == "object" and schema["additionalProperties"] is False


def test_a_ruleset_without_a_spec_schema_still_works() -> None:
    class Bare:
        def new_character(self, spec: CharSpec, rng: Rng) -> dict[str, Any]:
            return {"spec": spec.data}

    assert spec_schema_of(Bare()) == {}
    assert build_character(Bare(), CharSpec(data={"anything": 1}), Rng(0)) == {
        "spec": {"anything": 1}
    }


@pytest.mark.parametrize("rid", BUILT_INS)
def test_a_check_says_what_a_player_may_see(rid: str) -> None:
    ruleset = registry.resolve(rid)
    sheet = ruleset.new_character(CharSpec(), Rng(0))
    result = ruleset.resolve_check(CheckRequest(sheet=sheet), Rng(3))
    view = result.player_view
    assert view is not None and view["dice"] and view["band"]
    assert isinstance(view["stat"], str)
    # only the actor's own numbers: nothing of the sheet beyond the stat it rolled
    assert not {"hp", "max_hp", "ac", "harm", "wounds", "xp"} & set(view)
