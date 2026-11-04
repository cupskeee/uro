-- D-102: a thread's lifecycle signal, kept by the projector from each beat's recorded `threads`
-- (D-95): how many beats brought it into play, and how many beats it has been live (active or
-- offered). Existing rows start at 0; a replay (a fork, an import) counts from what the beats
-- recorded. Snapshots taken before this carry no counts and are skipped (snapshot v2).
ALTER TABLE proj_threads ADD COLUMN developments INT NOT NULL DEFAULT 0;
ALTER TABLE proj_threads ADD COLUMN beats_live INT NOT NULL DEFAULT 0;
