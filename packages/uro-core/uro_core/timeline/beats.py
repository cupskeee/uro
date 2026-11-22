"""A saved beat with what its commit said (D-115, Uro Loom request 24).

The live play channel tells a client what the rules decided in a beat: each check's grade, the
beat's mode, what it created, the plots it moved. All of that is already recorded as events, so
the chronicle can say it again when the story is read back, on any branch (a fork carries it).
Pure: `beat_records` groups events the store read from a branch's history."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from uro_core.domain.events import BeatResolvedPayload

# The events a beat record is made from. A store reads these, oldest first.
BEAT_RECORD_EVENTS: tuple[str, ...] = (
    "BeatResolved",
    "CheckResolved",
    "ActorCreated",
    "PlaceCreated",
    "FactionCreated",
    "ThreadCreated",
    "ThreadStateChanged",
    "EncounterStarted",
    "EncounterTurnTaken",
    "EncounterJoined",
)
_CREATED = {
    "ActorCreated": ("actor", "actor_id", "name"),
    "PlaceCreated": ("place", "place_id", "name"),
    "FactionCreated": ("faction", "faction_id", "name"),
    "ThreadCreated": ("thread", "thread_id", "stakes"),
}
_FIGHT = frozenset({"EncounterStarted", "EncounterTurnTaken", "EncounterJoined"})


class BeatCheck(BaseModel):
    actor_id: str
    affordance: str
    outcome: str
    # what the ruleset lets a player see of the roll; kept only on the character the beat was
    # played as once `for_players` has run
    player_view: dict[str, Any] | None = None


class BeatCreated(BaseModel):
    kind: str  # "actor" | "place" | "faction" | "thread"
    id: str
    name: str  # a thread's stakes


class BeatThreadChange(BaseModel):
    thread_id: str
    stakes: str
    from_state: str  # "" for a plot made after the beat (by the world's rules)
    to_state: str


class BeatRecord(BaseModel):
    """One saved beat: its words, and the player-safe facts of its commit. `threads_changed`
    covers the beat and whatever followed it before the next beat (the world's rules reacting,
    days passing, the game moving a plot): what moved between this turn and the next."""

    beat: BeatResolvedPayload
    commit_id: str
    mode: str = "freeroam"  # "encounter" when the beat was a fight's
    checks: list[BeatCheck] = Field(default_factory=list)
    created: list[BeatCreated] = Field(default_factory=list)
    threads_changed: list[BeatThreadChange] = Field(default_factory=list)


def fight_checks(turns: list[dict[str, Any]]) -> list[BeatCheck]:
    """A player's own turn in a fight, with the roll their ruleset lets them see (D-118), kept
    like a check so the story read back still shows it. The roll is on the turn itself, or, in a
    ruleset that resolves a round at once, among another turn's `views` in the same beat (D-119)."""
    late: dict[str, dict[str, Any]] = {}
    for turn in turns:
        late.update(turn.get("views") or {})
    checks = []
    for turn in turns:
        if turn.get("chosen") is None:
            continue
        own_view = turn.get("player_view")
        other = late.get(turn["actor_id"], {}) if own_view is None else {}
        view = own_view or other.get("player_view")
        if view is not None:
            checks.append(
                BeatCheck(
                    actor_id=turn["actor_id"],
                    affordance=turn["action"],
                    outcome=other.get("result") or turn.get("result") or "",
                    player_view=view,
                )
            )
    return checks


def beat_records(
    rows: list[tuple[str, str, dict[str, Any]]], stakes: dict[str, str]
) -> list[BeatRecord]:
    """Group `(commit_id, event_type, payload)` rows, oldest first, into beat records. A commit
    with a BeatResolved is that beat's; the commits after it, up to the next beat's, are what
    followed it. `stakes` names each thread (by id) for the plots that moved."""
    by_commit: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for commit_id, event_type, payload in rows:
        by_commit.setdefault(commit_id, []).append((event_type, payload))
    records: list[BeatRecord] = []
    moved: list[dict[str, tuple[str, str]]] = []  # per record: thread → (first from, last to)
    for commit_id, events in by_commit.items():
        beat = next((p for t, p in events if t == "BeatResolved"), None)
        if beat is None and not records:
            continue  # something before the first beat read: nobody's
        own = beat is not None
        if beat is not None:
            records.append(
                BeatRecord(
                    beat=BeatResolvedPayload(**beat),
                    commit_id=commit_id,
                    mode="encounter" if any(t in _FIGHT for t, _ in events) else "freeroam",
                )
            )
            moved.append({})
        record, changes = records[-1], moved[-1]
        for event_type, p in events:
            if own and event_type == "CheckResolved":
                record.checks.append(
                    BeatCheck(
                        actor_id=p["actor_id"],
                        affordance=p["affordance"],
                        outcome=p["outcome"],
                        player_view=p.get("player_view"),
                    )
                )
            elif own and event_type in _CREATED:
                kind, id_key, name_key = _CREATED[event_type]
                record.created.append(BeatCreated(kind=kind, id=p[id_key], name=p[name_key]))
            elif event_type == "ThreadCreated":  # made after the beat, by what followed it
                changes[p["thread_id"]] = ("", p.get("state") or "dormant")
            elif event_type == "ThreadStateChanged":
                first = changes.get(p["thread_id"], (p.get("from_state", ""), ""))[0]
                changes[p["thread_id"]] = (first, p["to_state"])
        if own:
            record.checks += fight_checks([p for t, p in events if t == "EncounterTurnTaken"])
    for record, changes in zip(records, moved, strict=True):
        made = {c.id for c in record.created if c.kind == "thread"}
        record.threads_changed = [
            BeatThreadChange(
                thread_id=tid, stakes=stakes.get(tid, ""), from_state=before, to_state=after
            )
            for tid, (before, after) in changes.items()
            if before != after and tid not in made
        ]
    return records


def for_players(records: list[BeatRecord]) -> list[BeatRecord]:
    """The records as a player may read them: a check's `player_view` stays only on the character
    the beat was played as (`BeatResolved.pc_actor_id`), as the play channel sent it. An NPC's
    numbers are dropped, and so is every view on a beat that doesn't say who played it (one from
    before D-115): which character a participant had then isn't known."""
    out = []
    for record in records:
        own = record.beat.pc_actor_id
        checks = [
            c if c.player_view is None or (own and c.actor_id == own)
            else c.model_copy(update={"player_view": None})
            for c in record.checks
        ]  # fmt: skip
        out.append(record.model_copy(update={"checks": checks}))
    return out


def without_views(records: list[BeatRecord]) -> list[BeatRecord]:
    """The records with nobody's dice: for a read that isn't one table's."""
    return [
        r.model_copy(
            update={"checks": [c.model_copy(update={"player_view": None}) for c in r.checks]}
        )
        for r in records
    ]
