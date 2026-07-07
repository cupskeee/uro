-- Phase 2 inc 2.2: PC-binding projection (docs/02, 12). "Is this actor a PC?" is
-- answered per-branch by the campaign's PCBound/PCReleased history — NOT a global flag,
-- because the same actor_id is a PC on the fork where the player continues and an
-- ordinary NPC on a sibling fork (the meteor test's retired wizard). `active` flips to
-- false on PCReleased (campaign end); the row stays as history ("was a PC in campaign A").
CREATE TABLE proj_pcs (
    branch_id      TEXT NOT NULL,
    campaign_id    TEXT NOT NULL,
    actor_id       TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    active         BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (branch_id, campaign_id, actor_id)
);
CREATE INDEX proj_pcs_active_idx ON proj_pcs (branch_id, actor_id) WHERE active;
