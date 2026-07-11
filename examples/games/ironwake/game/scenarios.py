"""Battle scenarios — authored 10x10 maps + enemy compositions (TASK B.1/B.7).

Map legend: '.' floor · '#' wall · '+' cover · 'C' company deploy cell · 'E' enemy deploy cell ·
'o' the objective tile (floor). Deploy cells fill in reading order (top-left to bottom-right),
so deployment is deterministic. The scenario knows nothing about Uro — enemy actor ids are
assigned by the season layer and passed in.
"""

from __future__ import annotations

from dataclasses import dataclass

from ironwake.game.battle import Battle, Board
from ironwake.game.units import ENEMY_TEMPLATES, MERC_CLASSES, Unit


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    title: str
    map_rows: tuple[str, ...]
    enemies: tuple[tuple[str, str], ...]  # (template class, unit name) in deploy order
    feature: str = ""  # the objective's name, for the "held the {feature}" feat
    hold_rounds: int = 0  # Defend: end-of-round objective check
    collapse_round: int = 0  # Desperate Stand: the site kills everything still on the board
    max_deploy: int = 6


def _parse(
    map_rows: tuple[str, ...],
) -> tuple[Board, list[tuple[int, int]], list[tuple[int, int]], tuple[int, int] | None]:
    company_cells: list[tuple[int, int]] = []
    enemy_cells: list[tuple[int, int]] = []
    objective: tuple[int, int] | None = None
    clean: list[str] = []
    for y, row in enumerate(map_rows):
        out = []
        for x, ch in enumerate(row):
            if ch == "C":
                company_cells.append((x, y))
                ch = "."
            elif ch == "E":
                enemy_cells.append((x, y))
                ch = "."
            elif ch == "o":
                objective = (x, y)
                ch = "."
            out.append(ch)
        clean.append("".join(out))
    return Board(clean), company_cells, enemy_cells, objective


def build_battle(
    scenario: Scenario,
    mercs: list[tuple[str, str, str]],  # (uid, name, merc class)
    enemy_ids: list[str],  # uids matching scenario.enemies, in order
    seed: int,
) -> Battle:
    board, company_cells, enemy_cells, objective = _parse(scenario.map_rows)
    if len(mercs) > len(company_cells):
        raise ValueError(f"{scenario.scenario_id}: {len(mercs)} mercs, {len(company_cells)} cells")
    if len(scenario.enemies) > len(enemy_cells):
        raise ValueError(f"{scenario.scenario_id}: too few enemy deploy cells")
    if len(enemy_ids) != len(scenario.enemies):
        raise ValueError(f"{scenario.scenario_id}: enemy id/spec count mismatch")
    units = [
        Unit(uid=uid, name=name, team="company", spec=MERC_CLASSES[cls], pos=company_cells[i])
        for i, (uid, name, cls) in enumerate(mercs)
    ]
    units.extend(
        Unit(
            uid=enemy_ids[i], name=name, team="enemy", spec=ENEMY_TEMPLATES[cls], pos=enemy_cells[i]
        )
        for i, (cls, name) in enumerate(scenario.enemies)
    )
    return Battle(
        board=board,
        units=units,
        seed=seed,
        objective=objective,
        hold_rounds=scenario.hold_rounds,
        collapse_round=scenario.collapse_round,
    )


# ---------------------------------------------------------------------------------------------
# The season's authored scenarios. Each map is exactly 10x10.
# ---------------------------------------------------------------------------------------------

GRANARY = Scenario(
    scenario_id="granary",
    title="Rats in the Granary",
    map_rows=(
        "##########",
        "#C.......#",
        "#C..+....#",
        "#C....E..#",
        "#C.+..E..#",
        "#....+E..#",
        "#C....E..#",
        "#C.+.....#",
        "#........#",
        "##########",
    ),
    enemies=(
        ("Raider", "Raider Osric"),
        ("Raider", "Raider Tam"),
        ("Raider", "Raider Hewel"),
        ("Raider", "Raider Bryce"),
    ),
)

FERRY_LANDING = Scenario(
    scenario_id="ferry-landing",
    title="Hold the Ferry Landing",
    map_rows=(
        "##########",
        "#....##..#",
        "#.Co...E.#",
        "#.C....E.#",
        "#.C..+.E.#",
        "#.C..+.E.#",
        "#.C.....E#",
        "#.C..##.E#",
        "#........#",
        "##########",
    ),
    enemies=(
        ("Raider", "Raider Colm"),
        ("Raider", "Raider Dagny"),
        ("Raider", "Raider Ferro"),
        ("Wolf", "the war-hound Snagg"),
        ("Raider", "Raider Ulfe"),
        ("Raider", "Raider Vann"),
    ),
    feature="the ferry gate",
    hold_rounds=4,  # the raiders break off early — routed-alive enemies are WITNESSES
)

WINTER_PACK = Scenario(
    scenario_id="winter-pack",
    title="The Winter Pack",
    map_rows=(
        "..........",
        ".C...+...E",
        ".C.......E",
        ".C...#...E",
        ".C..##...E",
        ".....#....",
        ".C+......E",
        "..........",
        ".C........",
        "..........",
    ),
    enemies=(
        ("Wolf", "the grey alpha"),
        ("Wolf", "Winter wolf (one-eye)"),
        ("Wolf", "Winter wolf (scarred)"),
        ("Wolf", "Winter wolf (lame)"),
        ("Wolf", "Winter wolf (black)"),
    ),
)

WEST_ROAD_HEADHUNT = Scenario(
    scenario_id="west-road",
    title="The Red Captain at the Ford",
    map_rows=(
        "##########",
        "#C.......#",
        "#C..+....#",
        "#C.......#",
        "#C...+E..#",
        "#C....E..#",  # Vorlund's deploy cell (enemy order maps him here)
        "#C..+.E..#",
        "#........#",
        "#........#",
        "##########",
    ),
    enemies=(
        ("Raider", "Raider Grimm"),
        ("Vorlund", "Captain Vorlund"),
        ("Raider", "Raider Sorrel"),
    ),
)

BRIDGE_BRUTES = Scenario(
    scenario_id="bridge",
    title="Brutes at the Tollbridge",
    map_rows=(
        "##########",
        "#C...#...#",
        "#C...#..E#",
        "#C.......#",
        "#C..+..E.#",
        "#C.......#",
        "#C...#.E.#",
        "#....#...#",
        "#........#",
        "##########",
    ),
    enemies=(
        ("Brute", "Bone-Breaker Gurth"),
        ("Brute", "Brute of the tollhouse"),
        ("Brute", "Marsh brute"),
    ),
)

SILENT_MILL = Scenario(
    scenario_id="silent-mill",
    title="The Silent Mill",
    map_rows=(
        "##########",
        "#..#..#..#",
        "#.C......#",
        "#...++...#",
        "#...++..E#",
        "#.C......#",
        "#..#..#.E#",
        "#........#",
        "#........#",
        "##########",
    ),
    enemies=(
        ("Brute", "the mill brute"),
        ("Brute", "the granary brute"),
    ),
    collapse_round=5,  # the blaze wins before either side can: a witnessless wipe is LIVE
    max_deploy=2,  # the Mill Watch: a two-blade detachment holds it (TASK inc 5, the silence)
)

VENGEANCE_ROAD = Scenario(
    scenario_id="vengeance-road",
    title="Vengeance on the West Road",
    map_rows=(
        "..........",
        ".C...+..E.",
        ".C......E.",
        ".C..##....",
        ".C..##..E.",
        ".C......E.",
        ".C.+......",
        "..........",
        "..........",
        "..........",
    ),
    enemies=(
        ("Raider", "Raider Marrow"),
        ("Raider", "Raider Pike"),
        ("Raider", "Raider Slate"),
        ("Brute", "the west-road brute"),
    ),
)

RED_CAMP = Scenario(
    scenario_id="red-camp",
    title="Storm the Red Camp",
    map_rows=(
        "##########",
        "#C.......#",
        "#C..+..E.#",
        "#C.....E.#",
        "#C..+..E.#",
        "#C.....E.#",
        "#C..+..E.#",
        "#....o.E.#",  # 'o': the palisade gate — end the storm holding it to earn the feat
        "#........#",
        "##########",
    ),
    enemies=(
        ("Raider", "Camp raider Wick"),
        ("Vorlund", "Captain Vorlund"),
        ("Brute", "Standard-Bearer Krull"),
        ("Raider", "Camp raider Jory"),
        ("Raider", "Camp raider Ash"),
    ),
    feature="the Red Band palisade",
)

GRAIN_BARGES = Scenario(  # the what-if fork's alternate final contract
    scenario_id="grain-barges",
    title="Escort the Grain Barges",
    map_rows=(
        "..........",
        ".C....+.E.",
        ".C......E.",
        ".C........",
        ".C....+.E.",
        ".C......E.",
        ".C........",
        "..........",
        "..........",
        "..........",
    ),
    enemies=(
        ("Raider", "River raider Eddis"),
        ("Raider", "River raider Kolb"),
        ("Wolf", "the barge-dog"),
        ("Raider", "River raider Nils"),
    ),
)
