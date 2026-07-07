"""Phase 3 inc 3.1: the ruleset port + Uro Basic — pure, deterministic, no LLM (docs/06).

The load-bearing guarantee is REPLAY DETERMINISM (docs/10): the same seed + the same action
sequence must produce byte-identical rolls, effects, and end state — that is what makes
dry-run, recorded-response replay, and encounter debugging possible.
"""

from uro_core.rulesets.base import (
    Action,
    Award,
    CharSpec,
    CheckRequest,
    Combatant,
    EncounterCtx,
    EncounterState,
    Sheet,
)
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic


def _sheet(rs: UroBasic, abilities: dict[str, int], tier: int = 1) -> Sheet:
    return rs.new_character(CharSpec(abilities=abilities, weapon_tier=tier), Rng(0))


# --- seeded RNG determinism ---


def test_rng_same_seed_same_sequence() -> None:
    a, b = Rng(7), Rng(7)
    seq_a = [a.d20() for _ in range(20)]
    seq_b = [b.d20() for _ in range(20)]
    assert seq_a == seq_b
    assert all(1 <= x <= 20 for x in seq_a)
    # different seeds should (overwhelmingly) diverge
    assert [Rng(8).d20() for _ in range(20)] != seq_a


def test_rng_roll_ranges() -> None:
    r = Rng(3)
    assert all(1 <= r.die(6) <= 6 for _ in range(50))
    assert all(2 <= Rng(i).roll(2, 6) <= 12 for i in range(50))


# --- character model ---


def test_new_character_defaults() -> None:
    rs = UroBasic()
    sheet = rs.new_character(CharSpec(), Rng(0))
    # defaults: STR/DEX/CON 12 (+1), rest 10. L1 hp = 8 + CON mod(1) = 9; AC = 10 + DEX mod(1).
    assert sheet.level == 1 and sheet.proficiency == 2
    assert sheet.max_hp == 9 and sheet.hp == 9
    assert sheet.ac == 11
    assert sheet.modifier("STR") == 1 and sheet.modifier("INT") == 0


def test_progression_levels_up_and_heals() -> None:
    rs = UroBasic()
    sheet = rs.new_character(CharSpec(abilities={"CON": 14}), Rng(0))  # CON +2
    l1_hp = sheet.max_hp  # 8 + 2 = 10
    leveled = rs.progression(sheet, Award(levels=2))
    assert leveled.level == 3
    assert leveled.max_hp == l1_hp + 2 * (5 + 2)  # +2 levels of (5+CON)
    assert leveled.hp == leveled.max_hp  # level-up restores to full

    assert rs.progression(leveled, Award(levels=9)).level == 5  # capped at level 5


def test_new_character_clamps_to_level_cap() -> None:
    # Review fix: Uro Basic tops out at 5; a higher-level spec clamps rather than minting an
    # off-spec sheet (the cap is Uro Basic's rule, not baked into the generic port).
    rs = UroBasic()
    sheet = rs.new_character(CharSpec(level=50), Rng(0))
    assert sheet.level == 5
    assert sheet.max_hp == rs.new_character(CharSpec(level=5), Rng(0)).max_hp


def test_progression_no_op_award_does_not_heal() -> None:
    # Review fix: only a real level-up restores HP — a zero/capped award must not heal.
    rs = UroBasic()
    wounded = rs.new_character(CharSpec(), Rng(0)).model_copy(update={"hp": 1})
    assert rs.progression(wounded, Award(levels=0)).hp == 1  # no-op award: still wounded
    healed = rs.progression(wounded, Award(levels=1))
    assert healed.level == 2 and healed.hp == healed.max_hp  # real level-up: healed to full


# --- free-roam checks ---


def test_resolve_check_advantage_and_disadvantage() -> None:
    rs = UroBasic()
    sheet = _sheet(rs, {"CHA": 16})  # +3
    # advantage takes the higher of two d20; disadvantage the lower. Same seed → same pair,
    # so the advantage total must be >= the disadvantage total.
    adv = rs.resolve_check(CheckRequest(sheet=sheet, ability="CHA", dc=15, advantage=True), Rng(5))
    dis = rs.resolve_check(
        CheckRequest(sheet=sheet, ability="CHA", dc=15, disadvantage=True), Rng(5)
    )
    assert adv.total >= dis.total
    assert adv.modifier == 3  # CHA 16 → +3
    assert "vs DC 15" in adv.trace


def test_resolve_check_success_threshold() -> None:
    rs = UroBasic()
    sheet = _sheet(rs, {"STR": 10})  # +0 modifier
    # find a seed whose first d20 is known-high vs a low DC → success; the trace agrees.
    res = rs.resolve_check(CheckRequest(sheet=sheet, ability="STR", dc=1), Rng(1))
    assert res.success is True  # DC 1 is always met (d20 >= 1)
    hard = rs.resolve_check(CheckRequest(sheet=sheet, ability="STR", dc=99), Rng(1))
    assert hard.success is False  # DC 99 is never met
    assert res.total == res.roll + res.modifier


# --- affordances & triggers (D-21) ---


def test_affordances_declare_triggers_and_encounter_starter() -> None:
    rs = UroBasic()
    by_id = {a.id: a for a in rs.affordances()}
    assert "persuade" in by_id and by_id["persuade"].trigger_categories == ["change_disposition"]
    assert by_id["attack"].starts_encounter is True
    assert by_id["attack"].trigger_categories == ["violence"]
    # every affordance names a real ability and is check-backed
    assert all(a.ability in ("STR", "DEX", "CON", "INT", "WIS", "CHA") for a in rs.affordances())


# --- encounter mode ---


def _encounter(rs: UroBasic, seed: int) -> EncounterState:
    pc = Combatant(actor_id="a:pc", team="party", sheet=_sheet(rs, {"STR": 14, "CON": 12}, tier=2))
    f1 = Combatant(actor_id="a:foe1", team="foes", sheet=_sheet(rs, {"STR": 10, "CON": 8}))
    f2 = Combatant(actor_id="a:foe2", team="foes", sheet=_sheet(rs, {"STR": 10, "CON": 8}))
    return rs.start_encounter(EncounterCtx(encounter_id="e:1", combatants=[pc, f1, f2]), Rng(seed))


def test_start_encounter_orders_by_initiative() -> None:
    rs = UroBasic()
    state = _encounter(rs, 11)
    assert set(state.order) == {"a:pc", "a:foe1", "a:foe2"}
    inits = [state.combatants[a].initiative for a in state.order]
    assert inits == sorted(inits, reverse=True)  # highest initiative first


def test_legal_actions_and_npc_focus_fire() -> None:
    rs = UroBasic()
    state = _encounter(rs, 11)
    # wound foe1 so npc/pc focus-fire targets it (lowest hp among conscious foes)
    state.combatants["a:foe1"].sheet.hp = 1
    specs = {s.kind: s for s in rs.legal_actions(state, "a:pc")}
    assert "attack" in specs and set(specs["attack"].targets) == {"a:foe1", "a:foe2"}
    assert {"defend", "flee"} <= set(specs)
    # the PC (as an NPC chooser would) focus-fires the weakest foe
    act = rs.npc_action(state, "a:pc", Rng(0))
    assert act.kind == "attack" and act.target_id == "a:foe1"


def _play_to_end(rs: UroBasic, seed: int) -> tuple[list[str], EncounterState]:
    """Drive a full fight deterministically: PCs and NPCs both focus-fire the weakest foe.
    Returns (effect-trace transcript, final state). Rng is seeded once and threaded, so the
    whole fight is a pure function of `seed`."""
    rng = Rng(seed)
    state = _encounter(rs, seed)
    transcript: list[str] = []
    for _ in range(200):  # cap guards against a non-terminating bug
        if rs.is_over(state) is not None or state.over:
            break
        actor_id = state.current_actor()
        action = rs.npc_action(state, actor_id, rng)  # same deterministic chooser for both sides
        state, effects = rs.resolve_action(state, action, rng)
        transcript.extend(f"{e.actor_id}:{e.kind}:{e.amount}:{e.trace}" for e in effects)
    return transcript, state


def test_full_encounter_terminates_with_an_outcome() -> None:
    rs = UroBasic()
    transcript, state = _play_to_end(rs, 42)
    outcome = rs.is_over(state)
    assert outcome is not None  # the fight actually ended (didn't hit the cap)
    assert outcome.winner_team in ("party", "foes")
    assert len(outcome.casualties) >= 1  # someone went down
    assert any(":damage:" in line for line in transcript)


def test_encounter_replays_identically_from_the_same_seed() -> None:
    # THE core guarantee: identical seed + identical action policy → byte-identical fight.
    rs = UroBasic()
    t1, s1 = _play_to_end(rs, 99)
    t2, s2 = _play_to_end(rs, 99)
    assert t1 == t2
    assert s1.model_dump() == s2.model_dump()
    # a different seed produces a different fight (rolls diverge)
    t3, _ = _play_to_end(rs, 100)
    assert t3 != t1


def test_is_over_only_when_a_team_is_wiped() -> None:
    rs = UroBasic()
    state = _encounter(rs, 11)
    assert rs.is_over(state) is None  # both teams up
    for foe in ("a:foe1", "a:foe2"):
        state.combatants[foe].sheet.hp = 0
    outcome = rs.is_over(state)
    assert outcome is not None and outcome.winner_team == "party"
    assert set(outcome.casualties) == {"a:foe1", "a:foe2"} and outcome.survivors == ["a:pc"]


def test_downed_combatant_never_acts_first() -> None:
    # Review fix: a downed high-DEX combatant would sort to order[0]; the first turn must still
    # seat on a conscious actor (a corpse must never get to attack).
    rs = UroBasic()
    down = Combatant(actor_id="a:down", team="party", sheet=_sheet(rs, {"DEX": 20}))
    down.sheet.hp = 0
    ally = Combatant(actor_id="a:ally", team="party", sheet=_sheet(rs, {"STR": 12}))
    foe = Combatant(actor_id="a:foe", team="foes", sheet=_sheet(rs, {"STR": 12}))
    for seed in range(6):
        state = rs.start_encounter(
            EncounterCtx(encounter_id="e", combatants=[down, ally, foe]), Rng(seed)
        )
        assert rs.is_over(state) is None  # two live teams
        assert state.combatants[state.current_actor()].sheet.conscious  # never a corpse


def test_encounter_state_is_independent_and_resolve_is_pure() -> None:
    # Review fix (aliasing): start_encounter owns its sheets, and resolve_action is pure wrt
    # its input state — mutating/resolving never leaks back into the caller's data.
    rs = UroBasic()
    pc = Combatant(actor_id="a:pc", team="party", sheet=_sheet(rs, {"STR": 16}, tier=2))
    foe = Combatant(actor_id="a:foe", team="foes", sheet=_sheet(rs, {"CON": 8}))
    ctx = EncounterCtx(encounter_id="e", combatants=[pc, foe])

    state = rs.start_encounter(ctx, Rng(1))
    state.combatants["a:foe"].sheet.hp = 0
    assert ctx.combatants[1].sheet.hp > 0  # the input ctx foe is untouched

    fresh = rs.start_encounter(ctx, Rng(1))
    before = fresh.combatants["a:foe"].sheet.hp
    rs.resolve_action(fresh, Action(kind="attack", actor_id="a:pc", target_id="a:foe"), Rng(9))
    assert fresh.combatants["a:foe"].sheet.hp == before  # input state survives resolve_action
