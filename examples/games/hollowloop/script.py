"""The scripted provider + the intent -> (narration, extraction) table. Deterministic, no key.

WHY THE PROVIDER IS *ARMED* RATHER THAN KEYED ON THE INTENT (a real finding, logged):
`hello_uro`'s ScriptedProvider serves a queue in beat order; the brief asks instead for a table
"keyed by the player's intent ... so the same intent replays identically in any loop". That is
not implementable: **the extractor prompt never contains the player's intent.**
`build_extractor_messages` (pipeline/extraction.py:92-112) sends only KNOWN ACTORS / KNOWN
CLAIMS / NARRATION — deliberate player-text isolation (extraction.py:10-12), i.e. an
anti-prompt-injection fence. So `complete(stage_tag="extractor")` cannot know which intent it is
extracting for; only `stream()` (the narrator) sees the intent, as the last `user` message.

So the GAME decides eligibility (place + segment + Codex prereqs), selects the script entry, and
ARMS the provider with it; `stream` serves that entry's narration and `complete` serves that
entry's extraction. Same (intent, eligibility) -> byte-identical beat, in any loop. The arming
handle is the game's own scripting layer, not shadow world state: nothing canonical lives here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from world import CLUES, NPC_NAMES, PLACE_NAMES

_NO_FACTS = '{"actors": [], "claims": []}'
_NO_PLAN = '{"intent_class": "action", "triggers": [], "mechanics": []}'


@dataclass(frozen=True)
class Beat:
    """One armed beat: the prose the 'model' returns and the facts it 'extracts'."""

    intent: str
    narration: str
    extraction: str = _NO_FACTS


def clue_extraction(clue_key: str) -> str:
    """The extraction JSON that commits a keystone clue as a durable claim.

    `provenance="narrator"` is what makes it `truth="true"` (extraction.py:177 — the caller
    cannot set truth), and `about` must carry the actor's NAME, never its id: `about` names are
    resolved through `find_actor_by_name` (extraction.py:175), so "a:aldis" would silently
    commit a dangling `name:a:aldis` token instead of binding to the Elder.
    """
    clue = CLUES[clue_key]
    return json.dumps(
        {
            "actors": [],
            "claims": [
                {
                    "statement": clue["statement"],
                    "about": clue["about"],
                    "provenance": "narrator",
                    "durable": True,
                }
            ],
        }
    )


class ScriptedProvider:
    """Three async methods (docs/04). Serves the armed beat; a real provider calls a model."""

    def __init__(self) -> None:
        self._armed: Beat | None = None
        self.served: list[str] = []  # intents served, for the golden test

    def arm(self, beat: Beat) -> None:
        self._armed = beat

    def _beat(self) -> Beat:
        if self._armed is None:  # pragma: no cover — a bug in the caller, never in play
            raise RuntimeError("the provider was not armed for this beat")
        return self._armed

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        beat = self._beat()
        self.served.append(beat.intent)
        yield beat.narration

    async def complete(self, req: CompletionRequest) -> str:
        if req.stage_tag == "planner":  # no ruleset is bound, so this is never reached
            return _NO_PLAN
        return self._beat().extraction  # the extractor

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


# --- the prose ---------------------------------------------------------------------------------

WAKE = (
    "You wake on the cold grass above the Vale of Mourn with the taste of ash in your mouth, "
    "and the village below is whole again. Smoke rises straight from the chimneys. Nobody is "
    "screaming yet."
)


def go_beat(place_id: str, segment_name: str) -> Beat:
    name = PLACE_NAMES[place_id]
    return Beat(
        intent=f"I walk to {name}.",
        narration=(
            f"You come to {name} in the {segment_name}. The Vale goes about its last day "
            "without knowing it, and the sky above the ridge is the colour of a held breath."
        ),
    )


def wait_beat(segment_name: str) -> Beat:
    return Beat(
        intent="I wait, and watch the sky.",
        narration=(
            f"You let the {segment_name} pass. The light shifts a degree; somewhere a shutter "
            "knocks. Above the ridge the sky keeps its terrible patience."
        ),
    )


def look_intent(place_id: str) -> str:
    return f"I take in {PLACE_NAMES[place_id]}."


# talk beats: (npc, clue_key or None) -> the prose. A clue beat's extraction commits the claim.
_CLUE_PROSE = {
    "K1": (
        "Elder Aldis unrolls the star-chart with a thumbnail and does not look up. 'An omen, "
        "they'll tell you. It is not an omen.' His finger stops on a mark near the horizon. "
        "'It is a stone. It has been falling for a thousand years, and it lands at last light.'"
    ),
    "K2": (
        "Chaplain Sela goes very still when you say the word 'star'. 'Then you know,' she says. "
        "'The Sky-Bell was not hung to call us to prayer. It was hung to answer. Ring it at the "
        "instant the star strikes — not before, not after — and the sound will hold the sky.'"
    ),
    "K3": (
        "Wren is lying on her belly at the well's lip, dropping pebbles into the dark to hear "
        "them land. 'I took the tower key,' she says, entirely unashamed. 'The Chaplain rings "
        "that bell for funerals and I hate it. It's in the well, on the ledge, where nobody "
        "looks.'"
    ),
    "K4": (
        "Harrow the Stranger is standing under the silent bell when you climb the tower stair. "
        "'You've seen it fall,' he says — not a question. 'Then you know the hour. Nightfall. "
        "Not dusk, not last light. The bell must ring at nightfall and at no other hour.'"
    ),
}

_FLAVOR = {
    "a:aldis": (
        "Elder Aldis is bent over his star-chart, and answers you with the half-attention of a "
        "man doing sums. There is nothing in him today you have not already taken."
    ),
    "a:sela": (
        "Chaplain Sela speaks kindly of the harvest and of your journey, and her eyes go to the "
        "bell-rope and away again. She has nothing to give you that you can use — not yet."
    ),
    "a:wren": (
        "Wren tells you a long, involved lie about a fox. She is delighted by it. She tells you "
        "nothing you need."
    ),
    "a:bryn": (
        "Bryn the Smith works the bellows and lets you talk. 'The bell-hammer's mine,' he "
        "allows, 'and it stays mine.' The forge is warm; the day goes on dying."
    ),
    "a:harrow": (
        "Harrow the Stranger watches the ridge-line and says nothing you can use. 'Come back "
        "when you know what it is,' he says, 'and I will tell you when.'"
    ),
}


def talk_beat(npc: str, clue_key: str | None) -> Beat:
    """A talk beat. With a clue: the extractor commits it. Without: prose only, nothing durable."""
    name = NPC_NAMES[npc]
    if clue_key is None:
        return Beat(intent=f"I talk with {name}.", narration=_FLAVOR[npc])
    return Beat(
        intent=f"I ask {name} about the Fall.",
        narration=_CLUE_PROSE[clue_key],
        extraction=clue_extraction(clue_key),
    )


SEARCH_WELL = Beat(
    intent="I climb down into the old well and feel along the ledge.",
    narration=(
        "The well is colder than the day. Your fingers find the ledge Wren spoke of, and on it, "
        "furred with damp, the long iron key to the bell tower. You put it in your coat."
    ),
)

RING = Beat(
    intent="I ring the Sky-Bell.",
    narration=(
        "The star comes down the sky like a seam splitting. You set both hands to the rope and "
        "you ring the Sky-Bell at the instant it strikes — and the sound goes up to meet it. "
        "The note holds. The sky holds. The star breaks apart high over the Vale of Mourn and "
        "comes down as nothing but light, and below you, in the square, the villagers look up "
        "into a rain of harmless fire, and do not understand why they are weeping. It is the "
        "first night the Vale has ever survived. It is the last night you will ever wake on the "
        "cold grass above it."
    ),
)

FALL = Beat(
    intent="I watch the sky at nightfall.",
    narration=(
        "At nightfall the star arrives. There is no sound at first — only the light, and the "
        "shadows of the houses thrown suddenly, hugely, the wrong way. Then the Vale of Mourn "
        "is gone, and the grass, and the well, and Wren, and the chapel with its silent bell. "
        "You are already waking on the cold grass, with the taste of ash, and the village below "
        "is whole again."
    ),
)


def fall_narration_facts() -> dict[str, Any]:
    """The Fall is HOST-AUTHORED (the Reaction Layer structurally cannot destroy a place, and a
    free-roam beat's extractor can only propose actors+claims). See loop.py commit_the_fall."""
    return {"statement": CLUES["K4"]["statement"], "about": CLUES["K4"]["about"]}
