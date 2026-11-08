-- D-112: how a campaign's fights play. 'auto' = resolved whole in one beat (D-29, every campaign
-- before this); 'turns' = a player character's turns are the player's own choice, one beat each.
ALTER TABLE campaigns ADD COLUMN encounters TEXT NOT NULL DEFAULT 'auto';
