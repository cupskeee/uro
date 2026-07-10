"""The examples/hello_uro embedding demo is a CONTRACT, not just docs — CI asserts its whole arc so
the public API it showcases can't silently rot. Deterministic (scripted provider, no key).
"""

import sys
from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore

# examples/ is not a package — put it on the path like a consumer's own project would import it.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "examples"))

import hello_uro


async def test_hello_uro_demo_shows_all_three_capabilities(store: PostgresEventStore) -> None:
    r = await hello_uro.demo(store)

    # (1) recall re-surfaced the fact established in beat 1
    assert "smugglers" in r["recalled_fact"].lower()

    # (2) the reaction layer fired on downtime: the feud woke + a module rumor spread + it reaches
    #     the narrator's active-thread context
    assert r["main_feud_state"] == "active"
    assert any("open war" in s for s in r["module_rumors"])
    assert r["active_plots_seen_by_narrator"]  # non-empty → the woken plot reaches recall

    # (3) branching: the what-if fork (taken before downtime) legitimately diverges from main
    assert r["whatif_feud_state"] == "dormant" and r["main_feud_state"] == "active"
