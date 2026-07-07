-- Phase 3 inc 3.3: items projection (docs/02, 12). Minimal ownership tracking so a lost
-- fight's loot (ItemTransferred) and a starting weapon (ItemCreated) persist into later
-- free-roam. Written by the projector on ItemCreated/ItemTransferred; HP damage lives in
-- proj_sheets (ActorDamaged mutates the sheet's hp), so no separate condition table yet.
CREATE TABLE proj_items (
    branch_id TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT '',
    owner_ref TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (branch_id, item_id)
);
CREATE INDEX proj_items_owner_idx ON proj_items (branch_id, owner_ref);
