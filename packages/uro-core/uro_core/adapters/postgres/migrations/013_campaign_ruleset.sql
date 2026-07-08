-- Phase 6 inc 6.3 (D-30): pin the bound ruleset on the campaign, so a later `play`/fork rebinds
-- the SAME ruleset (a d20 campaign resolves uro_basic; a PbtA campaign resolves uro_pbta). Before
-- this, only the CampaignStarted EVENT carried ruleset_id (and no version) — the play path ignored
-- it and hard-bound uro_basic. These columns make the binding queryable at get_campaign time.
ALTER TABLE campaigns ADD COLUMN ruleset_id TEXT NOT NULL DEFAULT '';
ALTER TABLE campaigns ADD COLUMN ruleset_version TEXT NOT NULL DEFAULT '';
