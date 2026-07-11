"""IRONWAKE's Reaction-Layer rule pack — and the rules the grammar REFUSED (stress goal 8).

The pack below is everything the declarative grammar (URO_INTEGRATION.md, docs/17) could carry
for this game: an event-triggered thread ratchet (deaths stir the war, step by step), a
loot-triggered war resolution, and downtime agendas (war rumors on a cadence, the war edge).

WISHED_RULES is the other half — the counter/arithmetic/quantifier rules IRONWAKE actually
wanted, each written EXACTLY as we would have authored it. SIX are structurally impossible
(pydantic's closed unions refuse the `when`/`then` node: no counters, no accumulating state,
no resources), so their state fell into game code (game/company.py's shadow counters). The
SEVENTH (RL-6, the quantified member-of trigger) is WORSE than refused: the grammar ACCEPTS it
— `Trigger.where` is a free-form dict matched verbatim against event payload fields — and the
rule then silently never fires, because no ActorDied payload carries an `actor.member_of` key.
A validates-but-inert rule is a sharper authoring footgun than a loud refusal; both classes are
demonstrated live in cli/season.py counter_wall and filed by log_refusals().
"""

from __future__ import annotations

from ironwake import frictionlog

# Scope wrinkle (logged as a gap in world/setup.py): a rule adding an at_war_with edge between
# TWO factions must have BOTH ends inside one scope, and a faction scope is "the faction + its
# member_of members" (rules_gauntlet._scope_refs). So the war edge rule needs a meta-faction —
# "the Marches theater" — that both belligerents are members of. Pure scaffolding for the fence.
THEATER = "f:the-marches"

IRONWAKE_RULE_PACK: dict = {
    "rules_api_version": 1,
    "rules": [
        # The war ratchet: the grammar cannot count deaths, but it CAN step a state machine on
        # distinct trigger beats. First blood stirs the war thread; blood answering blood opens
        # it. ("Go active after the company's THIRD winning contract" is in WISHED_RULES — refused.)
        {
            "id": "war-1-first-blood-stirs-the-marches",
            "trigger": {"event": "ActorDied"},
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "dormant"},
            "then": [{"do": "set_thread_state", "thread": "t:red-band-war", "to": "offered"}],
            "scope": {"thread": "t:red-band-war"},
        },
        {
            "id": "war-2-blood-answers-blood",
            "trigger": {"event": "ActorDied"},
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "offered"},
            "then": [{"do": "set_thread_state", "thread": "t:red-band-war", "to": "active"}],
            "scope": {"thread": "t:red-band-war"},
        },
        # The one declarative rule that RESOLVES the season: when the Red Band's standard is
        # looted (an ItemTransferred the Chronicler commits from the final battle's bundle),
        # the war breaks. A trigger `where` filter pins it to the exact item.
        {
            "id": "war-9-the-standard-falls",
            "trigger": {"event": "ItemTransferred", "where": {"item_id": "i:red-band-standard"}},
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "active"},
            "then": [{"do": "set_thread_state", "thread": "t:red-band-war", "to": "resolved"}],
            "scope": {"thread": "t:red-band-war"},
        },
    ],
    "agendas": [
        # The war also ESCALATES on downtime (TASK inc 6: the thread advances via agendas, not
        # only via battle-triggered rules): an offered war left to smoulder goes active on its
        # own cadence, whichever of this agenda / the blood-answers-blood rule fires first.
        {
            "id": "agenda-war-smoulders",
            "every_days": 20,
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "offered"},
            "then": [{"do": "set_thread_state", "thread": "t:red-band-war", "to": "active"}],
            "scope": {"thread": "t:red-band-war"},
        },
        # While the war is open, the Red Band raids — the world talks about it on a cadence.
        {
            "id": "agenda-red-band-raids",
            "every_days": 10,
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "active"},
            "then": [
                {
                    "do": "record_rumor",
                    "text": (
                        "They say Captain Vorlund's Red Band burned another grange on the "
                        "west road — the Marches bleed while the lords squabble."
                    ),
                    "subjects": ["a:vorlund"],
                }
            ],
            "scope": {"faction": "f:red-band"},
        },
        # Once the war thread is active, the factions formally go to war (the edge) — needing
        # the THEATER meta-faction so both ends sit inside one scope (see the module docstring).
        {
            "id": "agenda-war-drums",
            "every_days": 15,
            "when": {
                "kind": "all",
                "all": [
                    {"kind": "thread_state", "thread": "t:red-band-war", "state": "active"},
                    {
                        "kind": "not",
                        "cond": {
                            "kind": "edge_exists",
                            "src": "f:red-band",
                            "rel": "at_war_with",
                            "dst": "f:ironwake",
                        },
                    },
                ],
            },
            "then": [
                {"do": "add_edge", "src": "f:red-band", "rel": "at_war_with", "dst": "f:ironwake"}
            ],
            "scope": {"faction": THEATER},
        },
        # Peace rumor once the war resolves — the taverns exhale.
        {
            "id": "agenda-the-marches-exhale",
            "every_days": 10,
            "when": {"kind": "thread_state", "thread": "t:red-band-war", "state": "resolved"},
            "then": [
                {
                    "do": "record_rumor",
                    "text": (
                        "The Red Band's standard is taken and the warbands scatter — the "
                        "roads are open again, they say."
                    ),
                    "subjects": ["a:mira"],
                }
            ],
            "scope": {"faction": "f:ironwake"},
        },
    ],
}


# -----------------------------------------------------------------------------------------------
# THE REFUSAL LOG (stress goal 8) — rules IRONWAKE wanted, written exactly as we would author
# them. Every one needs a primitive the closed grammar does not have. The counters they imply
# now live in game/company.py (the shadow counters) — state Uro should own but structurally
# cannot. This log is the evidence input for the reserved WASM scripting tier (D-33 Stage B).
# -----------------------------------------------------------------------------------------------

WISHED_RULES: tuple[dict, ...] = (
    {
        "name": "war goes active after the company's 3rd winning contract",
        "missing": "a counter over event occurrences (count of EncounterEnded/win outcomes)",
        "where": "game/company.py Company.wins (+ cli/season.py pay logic)",
        "rule": """\
{ "id": "war-active-after-three-wins",
  "trigger": {"event": "EncounterEnded", "where": {"outcome.result": "win"}},
  "when": {"kind": "counter", "name": "ironwake_wins", "op": ">=", "value": 3},   # NO SUCH KIND
  "then": [{"do": "set_thread_state", "thread": "t:red-band-war", "to": "active"},
           {"do": "increment", "counter": "ironwake_wins"}],                      # NO SUCH ACTION
  "scope": {"thread": "t:red-band-war"} }""",
    },
    {
        "name": "reputation tier rises with the company's total kills",
        "missing": "an accumulator + threshold banding (arithmetic on persistent state)",
        "where": "game/company.py Company.total_kills (a write-only receipt — no reputation "
        "feature was built on it; the counter exists to show WHERE the state fell)",
        "rule": """\
{ "id": "reputation-from-total-kills",
  "trigger": {"event": "ActorDied"},
  "when": {"kind": "counter", "name": "kills_by:f:ironwake", "op": ">=", "value": 20},
  "then": [{"do": "record_rumor",
            "text": "The Ironwake Company has put a score of Red Band men in the ground.",
            "subjects": ["a:vorlund"]}],
  "scope": {"faction": "f:red-band"} }""",
    },
    {
        "name": "the war escalates every 5 dead raiders",
        "missing": "a modulo/threshold counter keyed on a FACTION's members (counter + join)",
        "where": "game/company.py Company.red_band_dead",
        "rule": """\
{ "id": "escalation-every-five-raiders",
  "trigger": {"event": "ActorDied", "where": {"actor.member_of": "f:red-band"}},  # NO JOIN
  "when": {"kind": "counter", "name": "red_band_dead", "op": "%", "value": 5},     # NO MODULO
  "then": [{"do": "record_rumor", "text": "The Red Band musters fresh spears in the hills.",
            "subjects": ["a:vorlund"]}],
  "scope": {"faction": "f:red-band"} }""",
    },
    {
        "name": "the bounty on Vorlund rises every time the Headhunt fails",
        "missing": "numeric state (a price) + arithmetic (+25 per failure); no resource primitive",
        "where": "game/company.py Company.bounty_failures (a write-only receipt — the pay table "
        "stayed flat; the counter exists to show WHERE the state fell)",
        "rule": """\
{ "id": "bounty-rises-on-failure",
  "trigger": {"event": "ClaimRecorded", "where": {"origin": "external", "subject": "a:vorlund"}},
  "then": [{"do": "adjust_value", "key": "bounty:a:vorlund", "delta": 25}],       # NO SUCH ACTION
  "scope": {"thread": "t:vorlund-bounty"} }""",
    },
    {
        "name": "desperation gossip when the living roster falls below 3",
        "missing": "an aggregate over a faction's members (COUNT of living member_of edges)",
        "where": "game/company.py Company.living() (+ cli/season.py town scene flavor)",
        "rule": """\
{ "id": "desperation-below-three-blades",
  "trigger": {"event": "ActorDied"},
  "when": {"kind": "member_count", "faction": "f:ironwake", "alive": true,
           "op": "<", "value": 3},                                                 # NO AGGREGATE
  "then": [{"do": "record_rumor", "text": "The Ironwake Company is down to its last blades.",
            "subjects": ["a:mira"]}],
  "scope": {"faction": "f:ironwake"} }""",
    },
    {
        "name": "trigger on ANY Red Band member's death — ACCEPTED by the grammar, silently "
        "inert at runtime (not a refusal: the sharper footgun)",
        "missing": "a join/quantifier — Trigger.where is a free dict[str,str] matched verbatim "
        "against payload fields (engines/rules.py:65), and ActorDied carries only "
        "actor_id/cause, so 'actor.member_of' VALIDATES and never matches; nothing "
        "at authoring time checks where-keys against the event catalog",
        "where": "world/rules.py war ratchet fires on ANY ActorDied instead (over-broad)",
        "rule": """\
{ "id": "red-band-death-stirs-the-band",
  "trigger": {"event": "ActorDied", "where": {"actor.member_of": "f:red-band"}},   # NO JOIN
  "then": [{"do": "record_rumor", "text": "The Red Band counts its dead and sharpens iron.",
            "subjects": ["a:vorlund"]}],
  "scope": {"faction": "f:red-band"} }""",
    },
    {
        "name": "pay the contract purse when the kill is CANON (economy tied to recorded truth)",
        "missing": "any resource/currency primitive at all; gold exists only in game code",
        "where": "cli/season.py _settle_contract (gold is a Company field)",
        "rule": """\
{ "id": "pay-on-canon-death",
  "trigger": {"event": "ActorDied", "where": {"actor_id": "a:rb-c1-raider-osric"}},
  "then": [{"do": "transfer_resource", "resource": "gold", "amount": 25,
            "from": "a:warlord-skane", "to": "f:ironwake"}],                       # NO SUCH ACTION
  "scope": {"faction": "f:ironwake"} }""",
    },
)


def log_refusals() -> None:
    """File every wished-for rule in the friction instrument (idempotent)."""
    for w in WISHED_RULES:
        frictionlog.refusal(
            name=w["name"], wished_rule=w["rule"], missing=w["missing"], where=w["where"]
        )
