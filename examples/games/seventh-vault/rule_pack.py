"""HEIST_RULE_PACK — the declarative Reaction Layer for The Seventh Vault, and its refusal log.

What the heist WANTED: a numeric heat meter fed by check outcomes ("every failed check +1 heat;
heat >= 3 -> lockdown"). What the grammar can express: neither half.

1. There is NO committed event for a check outcome. Free-roam checks are trace-only
   (pipeline/mechanics.py:29 resolve_mechanics returns CheckResult; nothing reaches append_beat)
   and no `CheckResolved` event type exists anywhere in domain/events.py. A trigger on
   {"event": "CheckResolved"} VALIDATES and then never fires (accepted-but-inert; probe in
   stress/s7_counters.py).
2. There are no counters/arithmetic/accumulating state (worldpack/rules.py conditions are
   compare-only), so "3 blunders -> lockdown" is inexpressible even if (1) existed.

So the alarm below escalates on the ONE event a stub free-roam beat commits — BeatResolved —
matched by `where` on the EXACT intent_text string of each scripted blunder. That is: the rules
pattern-match player prose because the engine gives them nothing mechanical to read. Each rule's
`when` gates on the alarm's current state, so escalation is one enum step per blunder beat and
the same trigger can never double-fire (rules evaluate against the pre-react projections:
engines/rules.py evaluates, THEN the gauntlet commits one module beat).

State vocabulary: the heist words (calm/suspicious/...) are punned onto the grammar's closed
ThreadState literal — see world.py. `to: "offered"` below MEANS "suspicious"; the grammar
rejects the real word (pydantic literal_error, gap table).
"""

from __future__ import annotations

from frictionlog import refusal
from world import (
    ALARM_STATES,
    ALARM_THREAD,
    CREW_FACTION,
    GUARD_FACTION,
    SCORE_STATES,
    SCORE_THREAD,
    WARDEN,
)

# The exact scripted intents the rules key on (players.py MUST send these verbatim — renaming a
# line of dialogue silently disarms a rule; that fragility is logged as a gap).
BLUNDER_GATE = "I force the rusted crossbar off the Outer Gate."
BLUNDER_GALLERY = "I knock over a display case in the Gallery."
BLUNDER_HUB = "I wedge the Security Hub door against the guard patrol."
GETAWAY_CLEAN = "I bar the chute grate behind us."
GETAWAY_BETRAYED = "I turn back for Sable and find only empty dark."

HEIST_RULE_PACK = {
    "rules_api_version": 1,
    "rules": [
        # --- the alarm ladder: one enum step per scripted blunder ---------------------------
        {
            "id": "alarm-1-gate-blunder",
            "trigger": {"event": "BeatResolved", "where": {"intent_text": BLUNDER_GATE}},
            "when": {
                "kind": "thread_state",
                "thread": ALARM_THREAD,
                "state": ALARM_STATES["calm"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": ALARM_THREAD,
                    "to": ALARM_STATES["suspicious"],
                }
            ],
            "scope": {"thread": ALARM_THREAD},
        },
        {
            "id": "alarm-2-gallery-blunder",
            "trigger": {"event": "BeatResolved", "where": {"intent_text": BLUNDER_GALLERY}},
            "when": {
                "kind": "thread_state",
                "thread": ALARM_THREAD,
                "state": ALARM_STATES["suspicious"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": ALARM_THREAD,
                    "to": ALARM_STATES["alerted"],
                }
            ],
            "scope": {"thread": ALARM_THREAD},
        },
        {
            "id": "alarm-3-hub-blunder",
            "trigger": {"event": "BeatResolved", "where": {"intent_text": BLUNDER_HUB}},
            "when": {
                "kind": "thread_state",
                "thread": ALARM_THREAD,
                "state": ALARM_STATES["alerted"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": ALARM_THREAD,
                    "to": ALARM_STATES["lockdown"],
                }
            ],
            "scope": {"thread": ALARM_THREAD},
        },
        # --- the score thread reacts to the prize moving (host-authored ItemTransferred) ----
        {
            "id": "score-1-prize-taken",
            "trigger": {"event": "ItemTransferred", "where": {"item_id": "i:prize"}},
            "when": {
                "kind": "thread_state",
                "thread": SCORE_THREAD,
                "state": SCORE_STATES["pending"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": SCORE_THREAD,
                    "to": SCORE_STATES["prize-taken"],
                }
            ],
            "scope": {"thread": SCORE_THREAD},
        },
        {
            "id": "score-2-clean-getaway",
            "trigger": {"event": "BeatResolved", "where": {"intent_text": GETAWAY_CLEAN}},
            "when": {
                "kind": "thread_state",
                "thread": SCORE_THREAD,
                "state": SCORE_STATES["prize-taken"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": SCORE_THREAD,
                    "to": SCORE_STATES["escaped"],
                }
            ],
            "scope": {"thread": SCORE_THREAD},
        },
        {
            "id": "score-3-betrayed-getaway",
            "trigger": {"event": "BeatResolved", "where": {"intent_text": GETAWAY_BETRAYED}},
            "when": {
                "kind": "thread_state",
                "thread": SCORE_THREAD,
                "state": SCORE_STATES["prize-taken"],
            },
            "then": [
                {
                    "do": "set_thread_state",
                    "thread": SCORE_THREAD,
                    "to": SCORE_STATES["betrayed"],
                }
            ],
            "scope": {"thread": SCORE_THREAD},
        },
        # --- a death in the vaults breeds guard-house gossip (fires on the Chronicler commit,
        #     because uro-server runs react() after POST /outcome: uro_server/app.py:81) ------
        {
            "id": "blood-in-the-vaults",
            "trigger": {"event": "ActorDied"},
            "then": [
                {
                    "do": "record_rumor",
                    "text": "They say the House lost guards under its own vaults the night "
                    "of the robbery, and the Warden has not been seen on the walls since.",
                    "subjects": [WARDEN],
                }
            ],
            "scope": {"faction": GUARD_FACTION},
        },
    ],
    "agendas": [
        # --- the legend spreads in the week after the job — which legend depends on the score
        {
            "id": "legend-of-the-crew",
            "every_days": 7,
            "when": {
                "kind": "thread_state",
                "thread": SCORE_THREAD,
                "state": SCORE_STATES["escaped"],
            },
            "then": [
                {
                    "do": "record_rumor",
                    "text": "They say a crew of four cracked the Seventh Vault, walked out "
                    "of a full lockdown, and vanished with the Heart.",
                    "subjects": ["a:vesna", "a:doran", "a:sable", "a:brakk"],
                }
            ],
            "scope": {"faction": CREW_FACTION},
        },
        {
            "id": "legend-of-the-ghost",
            "every_days": 7,
            "when": {
                "kind": "thread_state",
                "thread": SCORE_THREAD,
                "state": SCORE_STATES["betrayed"],
            },
            "then": [
                {
                    "do": "record_rumor",
                    "text": "They say the Ghost left her own crew to the House Guard and "
                    "walked out alone with the Heart of the Seventh Vault.",
                    "subjects": ["a:sable"],
                }
            ],
            "scope": {"faction": CREW_FACTION},
        },
    ],
}

# The agenda `when` above can only test the punned score state — but the LEGEND rules really
# want "when t:score reached its end state this week", i.e. an event-triggered agenda. Agendas
# have no trigger at all (worldpack/rules.py:195 AgendaRule), so the host must know to tick
# downtime only AFTER the getaway; tick earlier and the legend fires early or not at all.


def register_refusals() -> None:
    """The S7 refusal log: the rules the heist wanted, verbatim, and why each was refused.
    Registered by the arc at startup so every run prints its own receipts."""
    refusal(
        name="the heat METER (counter + threshold), not a 4-state enum",
        wished_rule="""{
  "id": "heat-accumulates",
  "trigger": {"event": "CheckResolved", "where": {"outcome": "failure"}},
  "then": [{"do": "increment_counter", "counter": "heat", "by": 1},
           {"do": "when_counter", "counter": "heat", "op": ">=", "value": 3,
            "then": {"do": "set_thread_state", "thread": "t:alarm", "to": "lockdown"}}],
  "scope": {"thread": "t:alarm"}
}""",
        missing="counters + arithmetic + a check-outcome event. Triple refusal: (a) no "
        "`increment_counter`/accumulating state in the Action union (worldpack/rules.py:156), "
        "(b) conditions are compare-only (no arithmetic), (c) `CheckResolved` does not exist — "
        "free-roam checks commit NOTHING (pipeline/mechanics.py:29 trace-only), so the trigger "
        "validates and is silently inert.",
        where="rule_pack.py HEIST_RULE_PACK — the 4-step enum ladder keyed on exact intent_text "
        "strings; the 'meter' semantics (how many blunders remain) exist nowhere.",
    )
    refusal(
        name="escalate on a FAILED check (any failed check, anywhere)",
        wished_rule="""{
  "id": "any-blunder-heats-the-job",
  "trigger": {"event": "CheckResolved", "where": {"outcome": "failure"}},
  "when": {"kind": "thread_state", "thread": "t:alarm", "state": "calm"},
  "then": [{"do": "set_thread_state", "thread": "t:alarm", "to": "suspicious"}],
  "scope": {"thread": "t:alarm"}
}""",
        missing="a committed check-outcome event. The mechanics gate resolves d20 checks but "
        "their outcomes never reach the event log (no event type; CheckResult.trace feeds only "
        "the narrator). The Reaction Layer is therefore BLIND to the entire mechanics layer.",
        where="rule_pack.py alarm-1/2/3 — `where: {intent_text: <exact scripted string>}`; "
        "renaming one line of player prose silently disarms the alarm.",
    )
    refusal(
        name="the heist's OWN state vocabulary (calm/suspicious/alerted/lockdown)",
        wished_rule="""{
  "id": "alarm-goes-suspicious",
  "trigger": {"event": "BeatResolved", "where": {"intent_text": "..."}},
  "when": {"kind": "thread_state", "thread": "t:alarm", "state": "calm"},
  "then": [{"do": "set_thread_state", "thread": "t:alarm", "to": "suspicious"}],
  "scope": {"thread": "t:alarm"}
}""",
        missing="an open (or pack-declared) thread-state vocabulary. ThreadState is "
        "Literal['dormant','offered','active','resolved','dead'] (domain/events.py:797, "
        "pinned by worldpack/rules.py:45,115); "
        "`to: 'suspicious'` is a pydantic literal_error — even though authored thread_created "
        "accepts ANY state string (domain/events.py:890). Rules can only speak the 5 words.",
        where="world.py ALARM_STATES/SCORE_STATES — the pun table (offered==suspicious, "
        "dead==lockdown) that every read-back must translate.",
    )
    refusal(
        name="mark the blunderer 'spotted' in the same rule that escalates the alarm",
        wished_rule="""{
  "id": "gallery-blunder-spots-the-muscle",
  "trigger": {"event": "BeatResolved", "where": {"intent_text": "I knock over a display case..."}},
  "then": [{"do": "set_thread_state", "thread": "t:alarm", "to": "alerted"},
           {"do": "add_edge", "src": "a:guard-9", "rel": "knows", "dst": "a:brakk"}],
  "scope": {"thread": "t:alarm"}
}""",
        missing="multi-dimension scope. Scope is exactly one of thread|faction|place "
        "(worldpack/rules.py:170); with `scope: {thread: t:alarm}` the add_edge's refs "
        "(guard, thief) are outside jurisdiction and the gauntlet drops the action "
        "(rules_gauntlet.py:42 _scope_refs). Splitting into a second faction-scoped rule fails "
        "too: guard and thief are in DIFFERENT factions and add_edge needs BOTH endpoints in "
        "one scope (rules_gauntlet.py — both src and dst must be allowed).",
        where="not built — the 'the guards know Brakk's face now' consequence was cut from the "
        "game; heist.py notes it at the gallery blunder hook.",
    )
    refusal(
        name="a lockdown that LIFTS after three quiet days (timer/decay)",
        wished_rule="""{
  "id": "the-house-stands-down",
  "trigger": {"event": "ThreadStateChanged", "where": {"thread_id": "t:alarm", "state": "dead"}},
  "then": [{"do": "after_days", "days": 3,
            "then": {"do": "set_thread_state", "thread": "t:alarm", "to": "dormant"}}],
  "scope": {"thread": "t:alarm"}
}""",
        missing="relative time / delayed actions. `world_day` conditions compare against an "
        "ABSOLUTE day number (worldpack/rules.py:70); nothing can express 'N days after event "
        "X'. Agendas fire on a fixed cadence with no memory of when the alarm tripped.",
        where="not built — the lockdown never lifts; the fiction ends before it matters.",
    )
    refusal(
        name="a weighted guard-response table (which squad answers the alarm)",
        wished_rule="""{
  "id": "who-answers-the-bell",
  "trigger": {"event": "ThreadStateChanged", "where": {"thread_id": "t:alarm", "state": "dead"}},
  "then": [{"do": "roll_table", "table": [
      {"weight": 3, "then": {"do": "record_rumor", "text": "the gate squad answered"}},
      {"weight": 1, "then": {"do": "record_rumor", "text": "the Warden himself came down"}}]}],
  "scope": {"faction": "f:house-guard"}
}""",
        missing="randomness / weighted tables. The grammar is fully deterministic by design "
        "(a feature for replay, a wall for simulation); any stochastic response must run in "
        "game code with the game's own RNG.",
        where="heist.py Skirmish — the guard response is the game's own seeded dice, reported "
        "back through the Chronicler.",
    )
