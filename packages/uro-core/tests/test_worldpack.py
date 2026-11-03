"""Phase 4 inc 4.1: world-pack parsing + the sufficiency check (docs/09). Deterministic — no LLM.

Parses the two example packs in `worlds/` (the rich Ashfall → runnable; the thin Thornwood →
flagged for a missing conflict seed) and unit-tests the sufficiency rubric + schema errors.
"""

from pathlib import Path

import pytest
from uro_core.errors import PackError
from uro_core.worldpack.models import WorldManifest, WorldPack
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.sufficiency import check_sufficiency

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


def test_parse_ashfall_pack() -> None:
    pack = parse_pack(WORLDS / "ashfall")
    assert pack.manifest.name == "Ashfall"
    assert pack.manifest.tone == ["grim", "low-magic", "political"]
    assert pack.manifest.content.rating == "mature"
    assert pack.manifest.content.enabled == ["violence", "horror"]
    assert pack.manifest.history.simulate_years == 200
    assert pack.manifest.llm_roles["narrator"] == "anthropic:claude-sonnet-5"
    assert {p.kind for p in pack.places} == {"region", "settlement", "site"}
    # D-76 added the salt road: three actors who know each other, a second plot, one rumor
    assert len(pack.actors) == 6 and len(pack.factions) == 2 and len(pack.threads) == 2
    assert len(pack.claims) == 1 and pack.rule_pack is not None and len(pack.rule_pack.agendas) == 3
    assert any(f.at_war_with for f in pack.factions)  # the Duchy/Saltborn war
    assert "overview.md" in pack.lore


def test_ashfall_is_runnable() -> None:
    report = check_sufficiency(parse_pack(WORLDS / "ashfall"))
    assert report.grade == "runnable"
    assert report.gaps == []


def test_thornwood_is_thin_only_missing_conflict() -> None:
    report = check_sufficiency(parse_pack(WORLDS / "thornwood"))
    assert report.grade == "thin"
    # the fixture is complete EXCEPT conflict seeds — that is the single gap the acceptance
    # (validate flags → backfill fills) depends on.
    assert [d.name for d in report.dimensions if not d.ok] == ["conflict"]
    assert any("conflict seeds" in g for g in report.gaps)


def _pack(**kw: object) -> WorldPack:
    base: dict[str, object] = {"manifest": WorldManifest(name="T", tone=["x"])}
    base.update(kw)
    return WorldPack(**base)  # type: ignore[arg-type]


def test_sufficiency_insufficient_without_geography_or_population() -> None:
    report = check_sufficiency(_pack())  # no places, no actors → structural failure
    assert report.grade == "insufficient"
    assert {"geography", "population"} <= {d.name for d in report.dimensions if not d.ok}


def test_generate_population_flag_satisfies_population() -> None:
    pack = _pack(manifest=WorldManifest(name="T", tone=["x"], generate_population=True))
    pop = next(d for d in check_sufficiency(pack).dimensions if d.name == "population")
    assert pop.ok


def test_parse_errors(tmp_path: Path) -> None:
    with pytest.raises(PackError):  # no world.toml
        parse_pack(tmp_path)
    (tmp_path / "world.toml").write_text("[world]\ntone = ['x']\n")
    with pytest.raises(PackError):  # missing [world].name
        parse_pack(tmp_path)
    (tmp_path / "world.toml").write_text('[world]\nname = "T"\n')
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "places.yaml").write_text("not: a-list\n")
    with pytest.raises(PackError):  # entities file is not a YAML list
        parse_pack(tmp_path)


def test_generate_population_counts_only_while_play_makes_people() -> None:
    # The flag means the world's people are made in play — the narrator names them and the
    # extractor records them (D-49/D-50) — so it stands in for seeded actors only while the
    # instance's extraction policy lets play create actors.
    pack = _pack(manifest=WorldManifest(name="T", tone=["x"], generate_population=True))
    on = next(d for d in check_sufficiency(pack).dimensions if d.name == "population")
    off = next(
        d
        for d in check_sufficiency(pack, play_creates_actors=False).dimensions
        if d.name == "population"
    )
    assert on.ok and "made in play" in on.detail
    assert not off.ok and "keeps play from creating actors" in off.detail
