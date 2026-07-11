"""The scripted court session — deterministic (narration, extraction-JSON) pairs per beat.

The game must not depend on prose quality, only on committed state — so a `ScriptedProvider`
(copied from examples/hello_uro) serves these pairs in beat order and the run is byte-stable
with no API key. Swap in a real provider behind --provider and the same intents narrate live.

The extractions are crafted to stress entity resolution on purpose (TASK.md §3 target 5):
claims refer to nobles COLLOQUIALLY ("the Marshal", "Aldric", "Garrick", "the Younger") so the
gauntlet must resolve them through authored aliases — and one beat introduces "the Salt Knight",
an alias nobody authored, to show the fragmentation failure mode live.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest


class ScriptedProvider:
    """Serves queued (narration, extraction-JSON) pairs in beat order; embeds via the stub
    hashing embedder so semantic memory works offline. One instance per scripted phase."""

    def __init__(self, beats: list[tuple[str, str]]) -> None:
        self._narrations = [n for n, _ in beats]
        self._extractions = [x for _, x in beats]
        self._n = self._x = 0

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        text = self._narrations[self._n]
        self._n += 1
        yield text

    async def complete(self, req: CompletionRequest) -> str:
        x = self._extractions[self._x]
        self._x += 1
        return x

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _x(actors: list[dict[str, str]], claims: list[dict[str, Any]]) -> str:
    return json.dumps({"actors": actors, "claims": claims})


def _dialogue(statement: str, about: list[str], speaker: str, confidence: float = 0.8) -> dict:
    return {
        "statement": statement,
        "about": about,
        "provenance": "dialogue",
        "speaker": speaker,
        "confidence": confidence,
        "durable": True,
    }


def _narrator(statement: str, about: list[str]) -> dict:
    return {"statement": statement, "about": about, "provenance": "narrator", "durable": True}


# --- The main line: (intent, narration, extraction) ---

MAIN_BEATS: list[tuple[str, str, str]] = [
    (  # 1 · whisper — plants a truth=unknown claim; aliases: "Lady Corvane", "the Marshal"
        "I have Lys whisper to Lady Corvane that the Marshal covets her salt-holdings.",
        "In a curtained alcove off the Sable Hall, Lys leans close to Lady Corvane. 'The Marshal "
        "counts your salt-pans in his sleep, my lady,' she murmurs. Aldrice Corvane's rings "
        "click once against her goblet, and she does not smile again that evening.",
        _x(
            [],
            [
                _dialogue(
                    "The Marshal covets the Corvane salt-holdings.",
                    ["the Marshal", "Lady Corvane"],
                    "Lys",
                    0.7,
                )
            ],
        ),
    ),
    (  # 2 · investigate — a narrator (truth=true) claim about the cult
        "I set watchers on the temple district to trace the Ashen Veil.",
        "By dusk three of my sparrows roost among the bell-ropes. Censer smoke drifts where no "
        "rite is scheduled, and Brother Sorrel walks the ash-yard long past midnight.",
        _x(
            [],
            [
                _narrator(
                    "Brother Sorrel walks the temple ash-yard past midnight, where censer smoke "
                    "drifts with no rite scheduled.",
                    ["Brother Sorrel"],
                )
            ],
        ),
    ),
    (  # 3 · blackmail
        "I confront Maren Argent with the forged crown notes and name my price.",
        "The Guildmistress reads the forgeries twice, which is once more than she needs. 'Half "
        "the Ledger's eyes are yours,' Maren says at last, 'and these burn tonight.'",
        _x(
            [],
            [
                _dialogue(
                    "Maren Argent has pledged the Ledger's eyes to the Spymaster in exchange "
                    "for the forged crown notes.",
                    ["Maren Argent"],
                    "Maren Argent",
                    0.9,
                )
            ],
        ),
    ),
    (  # 4 · bribe
        "I buy Captain Hurn's loyalty with crown silver.",
        "Hurn weighs the purse without opening it, which is how you know a man has taken coin "
        "before. 'The march levies answer to you first,' he says, 'and to the Marshal second.'",
        _x(
            [],
            [
                _dialogue(
                    "Captain Hurn's border levies now answer to the Spymaster first and the "
                    "Marshal second.",
                    ["Captain Hurn"],
                    "Captain Hurn",
                    0.8,
                )
            ],
        ),
    ),
    (  # 5 · sell-secret — "Aldric" must resolve to the Marshal (alias tiebreak), not the Younger
        "I sell the Marshal's letters to Lady Corvane.",
        "Aldrice reads the letters by a single candle. Her voice, when it comes, is winter off "
        "the salt flats: 'So Aldric would starve Saltport in a season. Let him try on an "
        "empty stomach.'",
        _x(
            [],
            [
                _dialogue(
                    "The Marshal's letters sketch a plan to starve Saltport within a season.",
                    ["Aldric", "Lady Corvane"],
                    "Lady Corvane",
                    0.9,
                )
            ],
        ),
    ),
    (  # 6 · incite-feud — the forged raid order; the game marks tension +2 and Vaelric unready
        "I forge a Corvane seal on an order to raid the Vaelric granaries on the march.",
        "By week's end the forgery has done its work: Vaelric outriders find Corvane wax on "
        "stolen requisitions, and the border march begins sharpening its knives in the open.",
        _x(
            [],
            [
                _narrator(
                    "Vaelric outriders found a raid order under Corvane wax, and the border "
                    "march arms for reprisal.",
                    ["Aldric", "Aldrice"],
                )
            ],
        ),
    ),
    (  # 7 · broker-marriage — "Aldric the Younger" must NOT collapse into the Marshal
        "I broker a betrothal: Aldric the Younger to a Dellmoor cousin — Vaelric swords for "
        "Oldkeep's grain.",
        "Aldous Dellmoor signs with a hand that shakes only a little. Aldric the Younger bows "
        "over a cousin's glove, and two Houses that shared nothing but a border now share "
        "a table.",
        _x(
            [],
            [
                _narrator(
                    "A betrothal binds Aldric the Younger to House Dellmoor — Vaelric swords "
                    "pledged for Oldkeep grain.",
                    ["Aldric the Younger", "Aldous Dellmoor"],
                )
            ],
        ),
    ),
    (  # 8 · the knights — Garret/Garrick/Gareth resolve by alias; "the Salt Knight" FRAGMENTS
        "I ask Ser Garret what Ser Garrick and Ser Gareth were doing on the Saltport quays.",
        "'Garrick swears it was guild business,' Garret shrugs. 'Gareth kept to the shadows and "
        "counted barrels. And there was a fourth — the men called him the Salt Knight — who "
        "paid the harbourmaster in Vaelric coin.'",
        _x(
            [{"name": "the Salt Knight", "role": "unknown knight on the quays"}],
            [
                _dialogue(
                    "Ser Garrick claimed guild business on the Saltport quays while Ser Gareth "
                    "kept watch on the barrels.",
                    ["Garrick", "Gareth"],
                    "Garret",
                    0.7,
                ),
                _dialogue(
                    "A knight called the Salt Knight paid the Saltport harbourmaster in "
                    "Vaelric coin.",
                    ["the Salt Knight"],
                    "Garret",
                    0.6,
                ),
            ],
        ),
    ),
]

# 9 · assassinate — previewed first (dry-run), then committed; the KILL itself goes through a
# Chronicler bundle and is downgraded by the trust ceiling. The prose claims nothing durable, so
# the only record of the King's "death" is the bundle's downgraded rumor — exactly the point.
ASSASSINATION_BEAT: tuple[str, str, str] = (
    "I send the knife: Lys slips into the King's bedchamber tonight.",
    "Dawn comes and the bells do not ring. The King coughs at his window, alive, while in the "
    "yards below men swear on their mothers that Halric was carried down cold before first "
    "light. Lys is gone before the gates open.",
    _x([], []),
)

# --- The fork line ("the-brokered-peace"): what-if the Spymaster had sued for peace? ---

FORK_BEATS: list[tuple[str, str, str]] = [
    (
        "I sue for the white peace: Vaelric and Corvane wed their quarrel to a truce at the Fords.",
        "On the trampled ground of the Fords, the Marshal and Lady Corvane exchange cold bread "
        "and colder salt. No one cheers. But the levies stand down, and the salt road opens "
        "by morning.",
        _x(
            [],
            [
                _narrator(
                    "Vaelric and Corvane sealed a white peace at the Fords, ending the "
                    "salt-road war.",
                    ["the Marshal", "Lady Corvane"],
                )
            ],
        ),
    ),
    (
        "I walk the salt quays and listen.",
        "Peace, of a kind: the quays smell of tar and fresh rope instead of ash. A Corvane "
        "factor laughs at something a Vaelric drover says, and neither reaches for a knife.",
        _x([], []),
    ),
]
