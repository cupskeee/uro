"""Phase 6 (OQ-13, D-30): Uro PbtA — the alien ruleset. Pure, deterministic, no LLM.

These tests are the port-generality proof: a structurally non-d20 ruleset (2d6 vs 7/10, a
harm clock, moves, no hp/ac) runs through the SAME generic port + runner as uro_basic. The
headline is `test_partial_success_is_a_distinct_third_band` — the graded outcome the old
`success: bool` could not hold, which is exactly the leak this whole phase removed.
"""

from uro_core.pipeline.encounter import run_encounter
from uro_core.rulesets.base import (
    Action,
    Award,
    CharSpec,
    CheckRequest,
    Combatant,
    EncounterCtx,
)
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_pbta import Sheet, UroPbtA

RS = UroPbtA()


def _sheet(stats: dict[str, int] | None = None) -> dict:
    return RS.new_character(CharSpec(data={"stats": stats} if stats else {}), Rng(0))


# --- character model: NO hp, NO ac (the d20 sheet shape must not have leaked in) ---


def test_new_character_has_pbta_shape_not_d20() -> None:
    sheet = _sheet()
    assert set(sheet) == {"stats", "harm", "conditions", "xp"}
    assert "hp" not in sheet and "ac" not in sheet  # the whole point
    assert set(sheet["stats"]) == {"forceful", "clever", "quick", "steady"}
    assert sheet["harm"] == 0 and sheet["conditions"] == []


def test_stat_is_the_modifier_no_derivation() -> None:
    # d20 derives mod=(score-10)//2; PbtA's stat IS the modifier. A +3 forceful adds exactly +3.
    sheet = _sheet({"forceful": 3})
    # over the 2d6 range, a +3 stat means totals run 5..15 (2+3 .. 12+3)
    totals = {
        RS.resolve_check(CheckRequest(sheet=sheet, stat="forceful"), Rng(s)).detail["total"]
        for s in range(200)
    }
    assert min(totals) >= 5 and max(totals) <= 15


# --- graded outcomes: the three-tier band (THE leak this phase removed) ---


def test_partial_success_is_a_distinct_third_band() -> None:
    sheet = _sheet({"forceful": 1})
    outcomes = {
        RS.resolve_check(CheckRequest(sheet=sheet, stat="forceful"), Rng(s)).outcome
        for s in range(80)
    }
    # miss / partial / full all occur — a binary success:bool literally cannot represent this.
    assert outcomes == {"miss", "partial", "full"}


def test_resolve_check_thresholds() -> None:
    # 6- miss, 7-9 partial, 10+ full — deterministic per (stat, dice).
    sheet = _sheet({"clever": 0})
    for seed in range(60):
        res = RS.resolve_check(CheckRequest(sheet=sheet, stat="clever"), Rng(seed))
        total = res.detail["total"]
        expected = "miss" if total <= 6 else "partial" if total <= 9 else "full"
        assert res.outcome == expected


# --- progression: advance by FAILING (mark XP on a miss), the anti-d20 axis ---


def test_progression_marks_xp_and_advances_on_threshold() -> None:
    sheet = _sheet({"forceful": 1, "clever": 1, "quick": 0, "steady": 0})
    marked = Sheet.model_validate(RS.progression(sheet, Award(data={"xp": 3})))
    assert marked.xp == 3 and marked.stats == sheet["stats"]  # below threshold: just accrues
    advanced = Sheet.model_validate(RS.progression(marked.model_dump(), Award(data={"xp": 2})))
    assert advanced.xp == 0  # 5 XP spent
    # the lowest stat (quick/steady tie → 'quick' by name) bumped by 1
    assert advanced.stats["quick"] == 1 and advanced.stats["steady"] == 0


# --- conflict: a move-exchange (no initiative, no rounds) through the generic runner ---


def test_conflict_terminates_and_replays_identically() -> None:
    def combatants() -> list[Combatant]:
        return [
            Combatant(actor_id="a:pc", team="party", sheet=_sheet({"forceful": 2})),
            Combatant(actor_id="a:foe", team="foes", sheet=_sheet({"forceful": 1})),
        ]

    e1, o1 = run_encounter(RS, combatants(), Rng(5), encounter_id="e:1")
    e2, o2 = run_encounter(RS, combatants(), Rng(5), encounter_id="e:1")
    assert [ev.payload for ev in e1] == [ev.payload for ev in e2]  # byte-identical replay
    assert o1.model_dump() == o2.model_dump()
    assert o1.winner_team in ("party", "foes") and len(o1.out_of_fight) >= 1
    assert e1[0].event_type == "EncounterStarted" and e1[-1].event_type == "EncounterEnded"
    # harm reaches the timeline as the opaque PbtA sheet (a harm clock), never an hp event
    sheet_events = [ev for ev in e1 if ev.event_type == "SheetUpdated"]
    assert sheet_events and all("harm" in ev.payload["sheet"] for ev in sheet_events)
    assert all("hp" not in ev.payload["sheet"] for ev in sheet_events)  # no d20 leak in persistence


def test_start_encounter_uses_no_initiative_roll() -> None:
    # Structural difference from d20: order is the combatants as handed in — no d20 roll consumed,
    # so the SAME rng is untouched by start_encounter (a d20 ruleset would have rolled initiative).
    combs = [
        Combatant(actor_id="a:z", team="party", sheet=_sheet()),
        Combatant(actor_id="a:a", team="foes", sheet=_sheet()),
    ]
    state = RS.start_encounter(EncounterCtx(encounter_id="e", combatants=combs), Rng(0))
    assert state.order == ["a:z", "a:a"]  # insertion order preserved, NOT sorted-by-initiative


def test_partial_in_conflict_leaves_a_standing_condition() -> None:
    # The acceptance's kernel: a 7-9 seize inflicts harm AND leaves the mover Exposed — a
    # persistent mechanical consequence a binary hit/miss cannot carry. Drive resolve_action with
    # a seed whose 2d6 lands in 7-9 for a +1 mover (dice 6-8).
    st = RS.start_encounter(
        EncounterCtx(
            encounter_id="e",
            combatants=[
                Combatant(actor_id="a:pc", team="party", sheet=_sheet({"forceful": 1})),
                Combatant(actor_id="a:foe", team="foes", sheet=_sheet({"forceful": 0})),
            ],
        ),
        Rng(0),
    )
    # find a seed that yields a partial for a +1 forceful mover
    partial_seed = next(
        s
        for s in range(100)
        if 6 <= Rng(s).roll(2, 6) <= 8  # dice 6-8 → total 7-9 with +1
    )
    _, effects = RS.resolve_action(
        st, Action(kind="seize_by_force", actor_id="a:pc", target_id="a:foe"), Rng(partial_seed)
    )
    assert effects and effects[0].kind == "partial"
    assert effects[0].payload["cost_condition"] == "Exposed"
