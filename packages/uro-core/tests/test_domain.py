import math

import pytest
from pydantic import ValidationError
from uro_core.domain.events import (
    BeatResolvedPayload,
    actor_created,
    actor_promoted,
    beat_resolved,
    belief_changed,
    world_genesis,
)
from uro_core.domain.hashing import compute_commit_hash


def test_world_genesis_payload() -> None:
    e = world_genesis("Ashfall")
    assert e.event_type == "WorldGenesis"
    assert e.caused_by.kind == "system"
    assert e.payload["world_name"] == "Ashfall"


def test_beat_resolved_shape() -> None:
    e = beat_resolved(beat_id="b1", participant_id="p1", intent_text="look", narration="You look.")
    assert e.event_type == "BeatResolved"
    assert e.caused_by.kind == "player_action"
    assert e.caused_by.participant_id == "p1"
    payload = BeatResolvedPayload(**e.payload)
    assert payload.intent_text == "look"
    assert payload.narration == "You look."


def test_commit_hash_is_deterministic_and_chained() -> None:
    e = world_genesis("W")
    h1 = compute_commit_hash(None, [e])
    assert h1 == compute_commit_hash(None, [e])  # same inputs → same hash
    assert compute_commit_hash("parent-hash", [e]) != h1  # parent changes the hash


@pytest.mark.parametrize("bad", [1.5, -0.1, math.nan, math.inf])
def test_belief_confidence_is_bounded(bad: float) -> None:
    # The sanctioned mint path rejects out-of-range / NaN / inf confidence, so
    # extractor garbage can never reach the projection (review Phase-1.1).
    with pytest.raises(ValidationError):
        belief_changed(actor_id="a", claim_id="c", confidence=bad)


@pytest.mark.parametrize("bad", [-1, 4, 99])
def test_actor_tier_is_bounded(bad: int) -> None:
    with pytest.raises(ValidationError):
        actor_created(actor_id="a", name="A", tier=bad)
    with pytest.raises(ValidationError):
        actor_promoted(actor_id="a", from_tier=1, to_tier=bad, reason="x")
