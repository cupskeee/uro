-- D-112: a campaign may choose its own game, an actor may carry a spec, an item its data.
-- campaigns.ruleset_config: the settings a campaign pinned for itself (a snapshot, so later edits
-- elsewhere never change it), or inherited from the story a fork came from. NULL = it follows its
-- world's [ruleset.config], as every campaign did before.
ALTER TABLE campaigns ADD COLUMN ruleset_config JSONB;
-- proj_actors.spec / threat: what the ruleset builds the actor's sheet from the first time one is
-- needed (a pack's opaque spec; the engine's neutral danger word from a pack or the extractor).
ALTER TABLE proj_actors ADD COLUMN spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE proj_actors ADD COLUMN threat TEXT NOT NULL DEFAULT '';
-- proj_items.data: opaque data a pack or a ruleset attached to an item; checks and fights see it.
ALTER TABLE proj_items ADD COLUMN data JSONB NOT NULL DEFAULT '{}'::jsonb;
