-- D-108: who made a place, a person or a faction, as threads already record it (011): "author"
-- (a pack), "ai_backfill" (backfill's inventions, for authors to review and platforms to show).
-- '' = not recorded: made in play, or before this column. Never inferred for old rows.
ALTER TABLE proj_places ADD COLUMN provenance TEXT NOT NULL DEFAULT '';
ALTER TABLE proj_actors ADD COLUMN provenance TEXT NOT NULL DEFAULT '';
ALTER TABLE proj_factions ADD COLUMN provenance TEXT NOT NULL DEFAULT '';
