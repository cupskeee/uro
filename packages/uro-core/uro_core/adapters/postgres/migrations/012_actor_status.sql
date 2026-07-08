-- Cross-phase review fix (P1+P3+P5): ActorDied previously only zeroed a character sheet's hp, so
-- a sheet-less casualty (a History-seeded NPC, or a Chronicler-reported death) left NO queryable
-- death trace — recall still surfaced it as alive/present. A lifecycle column on proj_actors gives
-- death a projection trace independent of the ruleset sheet.
ALTER TABLE proj_actors ADD COLUMN status TEXT NOT NULL DEFAULT 'alive';  -- alive | dead
