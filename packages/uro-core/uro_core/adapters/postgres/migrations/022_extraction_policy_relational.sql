-- D-50: emergent relational world-building. The extraction policy (021) gains two categories —
-- factions and threads — so play can create them too (an actor cascade-creates the faction it's
-- member_of and the place it's located_in). Default TRUE, so an existing instance keeps all-on
-- behavior (now also emergent factions/threads/edges).
ALTER TABLE extraction_policy
    ADD COLUMN extract_factions BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN extract_threads  BOOLEAN NOT NULL DEFAULT TRUE;
