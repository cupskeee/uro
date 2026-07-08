"""Phase 3 inc 3.1 / Phase 6: the ruleset port + Uro Basic — pure, deterministic, no LLM (docs/06).

The load-bearing guarantee is REPLAY DETERMINISM (docs/10): the same seed + the same action
sequence must produce byte-identical rolls, effects, and end state. Phase 6 (D-30) made the
port game-agnostic — sheets are opaque dicts, checks yield a graded `outcome`, encounter state
is opaque — so these tests exercise uro_basic through the generalized port + its own `Sheet`.
"""

from uro_core.rulesets.base import (
    Action,
    Award,
    CharSpec,
    CheckRequest,
    Combatant,
    EncounterCtx,
)
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import Sheet, UroBasic
from uro_core.rulesets.uro_basic.ruleset import ABILITIES

RS = UroBasic()


def _spec(abilities: dict[str, int] | None = None, tier: int = 1) -> CharSpec:
    data: dict[str, object] = {"weapon_tier": tier}
    if abilities:
        data["abilities"] = abilities
    return CharSpec(data=data)


def _sheet(abilities: dict[str, int], tier: int = 1) -> dict:
    return RS.new_character(_spec(abilities, tier), Rng(0))


# --- seeded RNG determinism ---


def test_rng_same_seed_same_sequence() -> None:
    a, b = Rng(7), Rng(7)
    seq_a = [a.d20() for _ in range(20)]
    seq_b = [b.d20() for _ in range(20)]
    assert seq_a == seq_b
    assert all(1 <= x <= 20 for x in seq_a)
    assert [Rng(8).d20() for _ in range(20)] != seq_a


def test_rng_roll_ranges() -> None:
    r = Rng(3)
    assert all(1 <= r.die(6) <= 6 for _ in range(50))
    assert all(2 <= Rng(i).roll(2, 6) <= 12 for i in range(50))


# --- character model (sheets are OPAQUE dicts at the port; uro_basic owns its Sheet shape) ---


def test_new_character_defaults() -> None:
    sheet = Sheet.model_validate(RS.new_character(CharSpec(), Rng(0)))
    # defaults: STR/DEX/CON 12 (+1), rest 10. L1 hp = 8 + CON mod(1) = 9; AC = 10 + DEX mod(1).
    assert sheet.level == 1 and sheet.proficiency == 2
    assert sheet.max_hp == 9 and sheet.hp == 9
    assert sheet.ac == 11
    assert sheet.modifier("STR") == 1 and sheet.modifier("INT") == 0


def test_progression_levels_up_and_heals() -> None:
    sheet = RS.new_character(_spec({"CON": 14}), Rng(0))  # CON +2
    l1_hp = Sheet.model_validate(sheet).max_hp  # 8 + 2 = 10
    leveled = Sheet.model_validate(RS.progression(sheet, Award(data={"levels": 2})))
    assert leveled.level == 3
    assert leveled.max_hp == l1_hp + 2 * (5 + 2)  # +2 levels of (5+CON)
    assert leveled.hp == leveled.max_hp  # level-up restores to full
    capped = Sheet.model_validate(RS.progression(leveled.model_dump(), Award(data={"levels": 9})))
    assert capped.level == 5  # capped at level 5


def test_new_character_clamps_to_level_cap() -> None:
    # Uro Basic tops out at 5; a higher-level spec clamps rather than minting an off-spec sheet
    # (the cap is Uro Basic's rule, NOT in the generic port — OQ-13/D-30).
    sheet = Sheet.model_validate(RS.new_character(CharSpec(data={"level": 50}), Rng(0)))
    assert sheet.level == 5
    assert (
        sheet.max_hp
        == Sheet.model_validate(RS.new_character(CharSpec(data={"level": 5}), Rng(0))).max_hp
    )


def test_progression_no_op_award_does_not_heal() -> None:
    # Only a real level-up restores HP — a zero/capped award must not heal.
    wounded = {**RS.new_character(CharSpec(), Rng(0)), "hp": 1}
    assert Sheet.model_validate(RS.progression(wounded, Award(data={"levels": 0}))).hp == 1
    healed = Sheet.model_validate(RS.progression(wounded, Award(data={"levels": 1})))
    assert healed.level == 2 and healed.hp == healed.max_hp  # real level-up: healed to full


# --- free-roam checks (graded outcome; d20 uses a 2-tier {failure,success} ladder) ---


def test_resolve_check_advantage_and_disadvantage() -> None:
    sheet = _sheet({"CHA": 16})  # +3
    # advantage takes the higher of two d20; disadvantage the lower. Same seed → same pair.
    adv = RS.resolve_check(
        CheckRequest(sheet=sheet, stat="CHA", difficulty="medium", modifiers={"advantage": True}),
        Rng(5),
    )
    dis = RS.resolve_check(
        CheckRequest(
            sheet=sheet, stat="CHA", difficulty="medium", modifiers={"disadvantage": True}
        ),
        Rng(5),
    )
    assert adv.detail["total"] >= dis.detail["total"]
    assert adv.detail["modifier"] == 3  # CHA 16 → +3
    assert "vs DC 15" in adv.trace


def test_resolve_check_graded_outcome_threshold() -> None:
    strong = _sheet({"STR": 30})  # +10 → beats an easy DC 10 on any d20
    weak = _sheet({"STR": 1})  # -5 → can never reach a hard DC 20
    for seed in range(30):
        good = RS.resolve_check(
            CheckRequest(sheet=strong, stat="STR", difficulty="easy"), Rng(seed)
        )
        bad = RS.resolve_check(CheckRequest(sheet=weak, stat="STR", difficulty="hard"), Rng(seed))
        assert good.outcome == "success"
        assert bad.outcome == "failure"
        assert good.detail["total"] == good.detail["roll"] + good.detail["modifier"]


# --- affordances & triggers (D-21) ---


def test_affordances_declare_triggers_and_encounter_starter() -> None:
    by_id = {a.id: a for a in RS.affordances()}
    assert "persuade" in by_id and by_id["persuade"].trigger_categories == ["change_disposition"]
    assert by_id["attack"].starts_encounter is True
    assert by_id["attack"].trigger_categories == ["violence"]
    # every affordance names a real ability stat and is check-backed
    assert all(a.stat in ABILITIES for a in RS.affordances())


# --- encounter mode (uro_basic's state is a private _Encounter; the runner sees it opaquely) ---


def _encounter(seed: int):
    pc = Combatant(actor_id="a:pc", team="party", sheet=_sheet({"STR": 14, "CON": 12}, tier=2))
    f1 = Combatant(actor_id="a:foe1", team="foes", sheet=_sheet({"STR": 10, "CON": 8}))
    f2 = Combatant(actor_id="a:foe2", team="foes", sheet=_sheet({"STR": 10, "CON": 8}))
    return RS.start_encounter(EncounterCtx(encounter_id="e:1", combatants=[pc, f1, f2]), Rng(seed))


def test_start_encounter_orders_by_initiative() -> None:
    state = _encounter(11)
    assert set(state.order) == {"a:pc", "a:foe1", "a:foe2"}
    inits = [state.fighters[a].initiative for a in state.order]
    assert inits == sorted(inits, reverse=True)  # highest initiative first


def test_legal_actions_and_npc_focus_fire() -> None:
    state = _encounter(11)
    state.fighters["a:foe1"].sheet.hp = 1  # wound foe1 → focus-fire targets it (lowest hp)
    specs = {s.kind: s for s in RS.legal_actions(state, "a:pc")}
    assert "attack" in specs and set(specs["attack"].targets) == {"a:foe1", "a:foe2"}
    assert {"defend", "flee"} <= set(specs)
    act = RS.npc_action(state, "a:pc", Rng(0))
    assert act.kind == "attack" and act.target_id == "a:foe1"


def _play_to_end(seed: int):
    """Drive a full fight through the port methods; return (transcript, final state)."""
    rng = Rng(seed)
    state = _encounter(seed)
    transcript: list[str] = []
    for _ in range(200):
        actor_id = RS.current_actor(state)
        if actor_id is None:
            break
        action = RS.npc_action(state, actor_id, rng)
        state, effects = RS.resolve_action(state, action, rng)
        transcript.extend(f"{e.actor_id}:{e.kind}:{e.trace}" for e in effects)
    return transcript, state


def test_full_encounter_terminates_with_an_outcome() -> None:
    transcript, state = _play_to_end(42)
    outcome = RS.is_over(state)
    assert outcome is not None
    assert outcome.winner_team in ("party", "foes")
    assert len(outcome.out_of_fight) >= 1  # someone went down
    assert any(":hit:" in line or ":down:" in line for line in transcript)


def test_encounter_replays_identically_from_the_same_seed() -> None:
    # THE core guarantee: identical seed + identical action policy → byte-identical fight.
    t1, s1 = _play_to_end(99)
    t2, s2 = _play_to_end(99)
    assert t1 == t2
    assert RS.sheets(s1) == RS.sheets(s2)
    t3, _ = _play_to_end(100)
    assert t3 != t1  # a different seed diverges


def test_is_over_only_when_a_team_is_wiped() -> None:
    state = _encounter(11)
    assert RS.is_over(state) is None  # both teams up
    for foe in ("a:foe1", "a:foe2"):
        state.fighters[foe].sheet.hp = 0
    outcome = RS.is_over(state)
    assert outcome is not None and outcome.winner_team == "party"
    assert set(outcome.out_of_fight) == {"a:foe1", "a:foe2"} and outcome.survivors == ["a:pc"]


def test_downed_combatant_never_acts_first() -> None:
    # A downed high-DEX combatant would sort to order[0]; the first turn must still seat on a
    # conscious actor (a corpse must never get to attack).
    down = Combatant(actor_id="a:down", team="party", sheet=_sheet({"DEX": 20}))
    down.sheet["hp"] = 0
    ally = Combatant(actor_id="a:ally", team="party", sheet=_sheet({"STR": 12}))
    foe = Combatant(actor_id="a:foe", team="foes", sheet=_sheet({"STR": 12}))
    for seed in range(6):
        state = RS.start_encounter(
            EncounterCtx(encounter_id="e", combatants=[down, ally, foe]), Rng(seed)
        )
        assert RS.is_over(state) is None  # two live teams
        current = RS.current_actor(state)
        assert current is not None and state.fighters[current].sheet.conscious  # never a corpse


def test_encounter_state_is_independent_and_resolve_is_pure() -> None:
    # start_encounter owns its sheets, and resolve_action is pure wrt its input state.
    pc = Combatant(actor_id="a:pc", team="party", sheet=_sheet({"STR": 16}, tier=2))
    foe = Combatant(actor_id="a:foe", team="foes", sheet=_sheet({"CON": 8}))
    ctx = EncounterCtx(encounter_id="e", combatants=[pc, foe])

    state = RS.start_encounter(ctx, Rng(1))
    state.fighters["a:foe"].sheet.hp = 0
    assert ctx.combatants[1].sheet["hp"] > 0  # the input ctx foe is untouched

    fresh = RS.start_encounter(ctx, Rng(1))
    before = fresh.fighters["a:foe"].sheet.hp
    RS.resolve_action(fresh, Action(kind="attack", actor_id="a:pc", target_id="a:foe"), Rng(9))
    assert fresh.fighters["a:foe"].sheet.hp == before  # input state survives resolve_action
