-- Phase 2 review fix (inc 2.1): branch names are unique per world.
--
-- Markers got UNIQUE(world_id, name) in 005; branches were left unguarded, so
-- `uro branch fork --name main` could mint a SECOND branch named 'main' — and the
-- world-main lookups (get_world / get_world_by_name) resolve via an unordered
-- `JOIN branches ... AND b.name='main'`, which then returns an arbitrary one and
-- silently targets the wrong timeline. Git's branch namespace is unique per repo;
-- Uro's is unique per world. Structural guarantee, not policy (the repo norm).
ALTER TABLE branches ADD CONSTRAINT branches_world_name_uniq UNIQUE (world_id, name);
