"""The golden-state contract (mirrors test_example_hello_uro.py's example-as-contract idea):
the full multiplayer heist — server subprocess, four WS clients, Chronicler skirmish, Reaction-
Layer alarm — replays byte-identically, and both endings land their asserted final state.

Each `arc.run_arc` call already hard-asserts the acceptance inside (stage self-checks +
heist.assert_ending); this test adds the two-run byte-determinism comparison and independent
spot-checks on the digest content.
"""

from __future__ import annotations

import arc
import pytest

pytestmark = pytest.mark.usefixtures("pg_available")


async def test_clean_heist_is_byte_deterministic_and_lands_the_acceptance() -> None:
    first = await arc.run_arc("clean")
    second = await arc.run_arc("clean")
    assert first == second, "two clean runs must produce byte-identical digests"
    fields = first.split("|")
    assert fields[0] == "clean"
    assert fields[1] == "lockdown"  # t:alarm — the heat the crew left behind
    assert fields[2] == "escaped"  # t:score
    assert fields[3] == "a:vesna"  # the Heart's final owner
    statuses = dict(kv.split("=") for kv in fields[4].split(","))
    assert statuses["a:guard-7"] == "dead"  # tier-0 casualty is canon
    assert statuses["a:warden"] != "dead"  # tier-3 casualty was downgraded
    assert statuses["a:guard-11"] != "dead"  # the clean run never touches the cellar
    assert "said to have fallen" in fields[5]  # the Warden testimony claim
    assert "a:guard-9" in fields[6] and "a:tapster" in fields[6]  # the rumor + its decay hop
    assert "crew of four" in fields[7]  # the clean legend


async def test_betrayal_heist_is_byte_deterministic_and_distinct() -> None:
    first = await arc.run_arc("betrayal")
    second = await arc.run_arc("betrayal")
    assert first == second, "two betrayal runs must produce byte-identical digests"
    fields = first.split("|")
    assert fields[0] == "betrayal"
    assert fields[1] == "lockdown"
    assert fields[2] == "betrayed"  # a DIFFERENT committed score state than the clean run
    assert fields[3] == "a:sable"  # the traitor holds the Heart
    statuses = dict(kv.split("=") for kv in fields[4].split(","))
    assert statuses["a:guard-11"] == "dead"  # the zero-witness scuffle
    assert "Ghost" in fields[7]  # the betrayal legend spread instead
