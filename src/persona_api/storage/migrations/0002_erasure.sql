-- Erasure as a per-person choice, and old values in the change log when somebody asks.
--
-- Additive only: three nullable columns and three partial indexes. Every existing row
-- reads back exactly as it did -- NULL purge_after is a tombstone, which is what every
-- forgotten row already was, and NULL old_value is an event that recorded no value,
-- which is what every event already was. See
-- docs/adr/0009-erasure-and-the-default-persona.md.

-- When a forgotten row may be destroyed.
--
-- Written once, by the request that forgets the row, from that person's
-- persona.erasure_mode and persona.grace_days -- and cleared if a field is set again
-- before then. The sweeper destroys what is due and asks nobody anything, which is why
-- it needs no user token, and why changing the setting later can never reach back and
-- reschedule (or unschedule) a row that was already forgotten.
--
-- NULL means never: a live row, or a tombstone.
ALTER TABLE fields ADD COLUMN purge_after TEXT;
ALTER TABLE notes  ADD COLUMN purge_after TEXT;

-- The sweeper's only query, as a seek over the few rows that are waiting. Partial, so
-- a table full of live rows and tombstones costs it nothing.
CREATE INDEX fields_due ON fields(purge_after) WHERE purge_after IS NOT NULL;
CREATE INDEX notes_due  ON notes(purge_after)  WHERE purge_after IS NOT NULL;

-- The value a change replaced, as JSON: a field's old value, or a note's old body.
--
-- Written only when the person had persona.log_values on at the time of the change,
-- and stripped back to NULL in the same transaction that destroys the row it describes
-- -- by the sweeper, by an immediate erasure, or by deleting the whole persona. Without
-- that, the log would be a second copy of what somebody asked to have destroyed.
ALTER TABLE events ADD COLUMN old_value TEXT;

-- What the strip has to find: the events about one persona that still hold a value.
CREATE INDEX events_holding_values ON events(account_id, profile, subject)
    WHERE old_value IS NOT NULL;
