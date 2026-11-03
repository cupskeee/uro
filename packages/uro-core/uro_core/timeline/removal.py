"""Removing a campaign (D-99): the one rule the CLI and the server share.

A campaign's story is its branch, so removing the campaign removes the branch (`delete_branch`:
its campaigns, projections, memories, metered calls and the commits only it reaches). Two cases
refuse instead of guessing: a campaign on its world's `main` branch (that branch is the world
itself — remove the world), and a branch other campaigns also play on (remove the branch, which
names them all). A privacy wipe, not an event: nothing records that it happened.
"""

from __future__ import annotations

from uro_core.ports.projections import EngineStore


async def delete_campaign(store: EngineStore, campaign_id: str) -> dict[str, int]:
    """Remove a campaign and its branch. KeyError if it doesn't exist; ValueError when the
    branch is `main` or shared with another campaign."""
    campaign = await store.get_campaign(campaign_id)
    if campaign is None:
        raise KeyError(campaign_id)
    branch = await store.get_branch(campaign.branch_id)
    if branch is None:
        raise KeyError(campaign.branch_id)
    if branch.name == "main":
        raise ValueError(
            "this campaign plays on its world's main branch, which holds the world itself: "
            "delete the world, or end the campaign"
        )
    others = [
        c.campaign_id
        for c in await store.list_campaigns(campaign.world_id)
        if c.branch_id == campaign.branch_id and c.campaign_id != campaign_id
    ]
    if others:
        raise ValueError(
            f"its branch {branch.name!r} is shared with campaign(s) {', '.join(others)}: "
            "delete the branch to remove them all"
        )
    return await store.delete_branch(campaign.branch_id)
