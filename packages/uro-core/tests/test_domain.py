from uro_core.domain.events import BeatResolvedPayload, beat_resolved, world_genesis
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
