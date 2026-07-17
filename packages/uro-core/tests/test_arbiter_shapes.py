"""D-38 (#9): arbiter shapes beyond round-robin — ProposalWindowArbiter (propose-then-act, G-10)
and VoteArbiter (consensus/vote, G-11). Both round-robin like PartyArbiter (the ring/rotation/G-17
departure fix are inherited); the coordination is session-only and non-canon — no beat is burned.
Deterministic — no server, no DB, no LLM. (The WS wiring is covered in uro-server's test_server.py.)
"""

from uro_core.session import AdmitDecision, ProposalWindowArbiter, VoteArbiter


async def _seat(arb: object, campaign: str, *participants: str) -> None:
    for p in participants:
        await arb.note_joined(campaign, p)  # type: ignore[attr-defined]


# --- ProposalWindowArbiter (G-10): a non-holder is QUEUED (surfaced), not silently refused ---


async def test_proposal_window_queues_a_non_holder_instead_of_refusing() -> None:
    arb = ProposalWindowArbiter()
    await _seat(arb, "c", "p1", "p2")  # p1 holds first (join order)
    assert await arb.admit("c", "p1", "act") == AdmitDecision.ADMITTED
    # the non-holder is QUEUED (a surfaced proposal), NOT NOT_YOUR_TURN — that is the whole shape
    assert await arb.admit("c", "p2", "we should split up") == AdmitDecision.QUEUED


async def test_proposal_window_rotates_on_commit_like_round_robin() -> None:
    arb = ProposalWindowArbiter()
    await _seat(arb, "c", "p1", "p2")
    await arb.beat_committed("c", "p1", "")  # the holder acted → token rotates to p2
    assert await arb.admit("c", "p2", "my turn") == AdmitDecision.ADMITTED
    assert await arb.admit("c", "p1", "a proposal") == AdmitDecision.QUEUED


async def test_proposal_window_holder_departure_hands_off_not_wedges() -> None:
    # G-17-class (inherited): the HOLDER leaving must hand the token to the successor, not wedge the
    # party. Ring [p1,p2,p3]; after p1 commits the holder is p2 (cursor 1); p2 (the holder) leaves.
    arb = ProposalWindowArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    await arb.beat_committed("c", "p1", "")  # holder → p2
    await arb.note_left("c", "p2")  # the holder leaves; p3 slides into the cursor slot
    assert await arb.admit("c", "p3", "go") == AdmitDecision.ADMITTED
    assert await arb.admit("c", "p1", "wait") == AdmitDecision.QUEUED


async def test_proposal_window_member_before_holder_leaves_keeps_holder() -> None:
    # G-17-class (inherited): a member strictly BEFORE the holder leaving must NOT step the holder.
    arb = ProposalWindowArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    await arb.beat_committed("c", "p1", "")  # holder → p2 (cursor 1)
    await arb.note_left("c", "p1")  # a member before the holder leaves → cursor 1→0, still p2
    assert await arb.admit("c", "p2", "still me") == AdmitDecision.ADMITTED


# --- VoteArbiter (G-11): a session-only tally, decided by strict plurality, no beat burned ---


async def test_vote_tallies_and_decides_by_strict_plurality() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    o1 = await arb.cast_vote("c", "p1", "loud")
    assert o1.decided is None and o1.tally == {"loud": 1}  # still collecting (1/3)
    o2 = await arb.cast_vote("c", "p2", "quiet")
    assert o2.decided is None  # 2/3 voted, running tie 1-1 → undecided
    o3 = await arb.cast_vote("c", "p3", "loud")
    assert o3.decided == "loud" and o3.tally == {"loud": 2, "quiet": 1}  # all voted, 2-1 plurality


async def test_vote_tie_stays_undecided() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2")
    await arb.cast_vote("c", "p1", "a")
    o = await arb.cast_vote("c", "p2", "b")  # everyone voted, but a 1-1 tie → no forced decision
    assert o.decided is None


async def test_vote_resets_after_a_decision() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2")
    await arb.cast_vote("c", "p1", "x")
    assert (await arb.cast_vote("c", "p2", "x")).decided == "x"  # 2-0 → decided
    o2 = await arb.cast_vote("c", "p1", "y")  # a fresh round starts empty (the tally reset)
    assert o2.tally == {"y": 1} and o2.decided is None


async def test_vote_revote_overwrites_own_choice() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2")
    await arb.cast_vote("c", "p1", "a")
    o = await arb.cast_vote("c", "p1", "b")  # same participant re-votes → overwrite, not a 2nd vote
    assert o.tally == {"b": 1} and o.decided is None  # still only 1 of 2 have voted


async def test_vote_departing_voter_is_dropped_so_the_round_can_resolve() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    await arb.cast_vote("c", "p3", "c")  # p3 votes, then fully disconnects
    await arb.note_left("c", "p3")
    await arb.cast_vote("c", "p1", "a")
    o = await arb.cast_vote("c", "p2", "a")  # the 2-member round resolves; p3's vote is gone
    assert o.decided == "a" and "c" not in o.tally


async def test_vote_resolves_when_the_last_holdout_departs_without_voting() -> None:
    # Review liveness fix: p1+p2 vote, p3 (the last holdout) DISCONNECTS without voting → the round
    # is now objectively complete (2/2 remaining), but cast_vote never re-fires. resolve_pending
    # (called by the server after note_left) surfaces the decision so it isn't silently stalled.
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    assert (await arb.cast_vote("c", "p1", "a")).decided is None
    assert (await arb.cast_vote("c", "p2", "a")).decided is None  # 2/3 — waiting on p3
    await arb.note_left("c", "p3")  # the holdout leaves without voting
    pending = await arb.resolve_pending("c")
    assert pending is not None and pending.decided == "a"  # the round now resolves


async def test_resolve_pending_is_none_when_still_undecided_or_tied() -> None:
    arb = VoteArbiter()
    await _seat(arb, "c", "p1", "p2", "p3")
    await arb.cast_vote("c", "p1", "a")
    assert await arb.resolve_pending("c") is None  # 1/3 voted → still pending, no spurious decision
    await arb.cast_vote("c", "p2", "b")
    await arb.note_left("c", "p3")  # now 2/2 but a 1-1 TIE → no forced decision
    assert await arb.resolve_pending("c") is None
