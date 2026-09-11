-- The whole schema, as one migration.
--
-- STRICT on every table. Without it SQLite will happily store a string in an INTEGER
-- column, and a row-mapper bug becomes data that reads back as the wrong type months
-- later rather than a failure at the write.
--
-- Datetimes are TEXT in the fixed-width format persona_api.storage.times writes, chosen
-- so that ORDER BY on the column is chronological. See that module for why.
--
-- Ids are opaque TEXT. Nothing here is guessable and nothing here is meaningful.

-- One persona per (account_id, profile).
--
-- The identity card, and deliberately small: a name, pronouns, a sentence. Everything
-- else an assistant might want to remember about itself is a field or a note, because
-- those grow without a migration and this does not.
--
-- account_id is the `sub` of a verified keyring token and is the only identity this
-- service has. profile is an opaque namespace string the caller supplies -- persona-api
-- CANNOT check that it exists in keyring, because keyring has no endpoint that would
-- answer. A typo makes a new empty persona rather than an error, which is why
-- list_personas exists. See docs/adr/0005-one-persona-per-profile.md.
CREATE TABLE personas (
    account_id   TEXT NOT NULL,
    profile      TEXT NOT NULL,
    persona_id   TEXT NOT NULL UNIQUE,
    display_name TEXT,
    pronouns     TEXT,
    summary      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, profile)
) STRICT;

-- The FTS docid allocator.
--
-- Both full-text indexes are keyed by an integer this table hands out, rather than by
-- the implicit rowid of the row being indexed. Two reasons, and the first is the one
-- that would have cost a weekend:
--
--   * VACUUM renumbers the implicit rowids of a table with no INTEGER PRIMARY KEY, and
--     `VACUUM INTO` is what docs/operations.md tells an operator to back up with. An
--     index joined on an implicit rowid would come back from that backup pointing at
--     the wrong rows -- silently, with every search returning somebody else's text.
--   * A counter that only ever goes up cannot reuse a docid freed by a delete, so a
--     stale index entry can never be mistaken for a live one.
--
-- Allocation is `UPDATE ... RETURNING next` inside the same transaction as the write,
-- which is race-free because storage.database serializes every transaction on one
-- thread.
CREATE TABLE fts_sequence (
    name TEXT    NOT NULL PRIMARY KEY,
    next INTEGER NOT NULL
) STRICT;

INSERT INTO fts_sequence (name, next) VALUES ('fields', 0), ('notes', 0);

-- Structured attributes, whose keys the assistant invents.
--
-- The key is normalized to snake_case before it ever reaches here, so "Favourite
-- Topics", "favourite-topics" and "FAVOURITE_TOPICS" are one row rather than three --
-- which is the whole anti-sprawl mechanism, and it works by making reuse easy rather
-- than by refusing a write.
--
-- value_type is derived from the value by the domain layer and never supplied by the
-- caller, so the two cannot disagree. description is required on create: it is what
-- lets the next write tell whether to reuse this key or invent one.
--
-- forgotten_at is a tombstone. DELETE on a field is a soft forget -- it stops appearing
-- in reads, and ?include_forgotten=true brings it back. Deleting the persona is the only
-- hard delete, and the foreign key below is what makes it one statement.
CREATE TABLE fields (
    account_id   TEXT    NOT NULL,
    profile      TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    field_id     TEXT    NOT NULL UNIQUE,
    seq          INTEGER NOT NULL UNIQUE,
    description  TEXT    NOT NULL,
    value_json   TEXT    NOT NULL,
    value_type   TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    asserted_by  TEXT    NOT NULL,
    pinned       INTEGER NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    forgotten_at TEXT,
    PRIMARY KEY (account_id, profile, key),
    FOREIGN KEY (account_id, profile)
        REFERENCES personas(account_id, profile) ON DELETE CASCADE
) STRICT;

-- Free text, for what does not fit a key.
--
-- kind is a closed set -- episode, observation, lesson -- for the same reason keyring's
-- Permission is closed: a typo'd free string is a note that can never be filtered for
-- again, and nobody notices until they go looking for it.
--
-- Keyed by note_id rather than by anything about its content, because two notes saying
-- the same thing at different times are two notes.
CREATE TABLE notes (
    note_id      TEXT    NOT NULL PRIMARY KEY,
    account_id   TEXT    NOT NULL,
    profile      TEXT    NOT NULL,
    seq          INTEGER NOT NULL UNIQUE,
    body         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    asserted_by  TEXT    NOT NULL,
    pinned       INTEGER NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    forgotten_at TEXT,
    FOREIGN KEY (account_id, profile)
        REFERENCES personas(account_id, profile) ON DELETE CASCADE
) STRICT;

-- The two full-text indexes.
--
-- Plain FTS5 tables, maintained by the stores inside the same transaction as the row --
-- not external-content, and not triggers.
--
--   * External content requires issuing 'delete' commands carrying the OLD values on
--     every update. Miss one and the index is silently corrupt, which is a well-known
--     footgun rather than a hypothetical.
--   * Triggers would have to render a JSON value to searchable text in SQL.
--
-- porter unicode61 so a search for "preferring" finds "prefer", and bm25() for ranking.
-- Both verified working on this stack; neither adds a dependency.
--
-- The indexed text for a field is a RENDERED PROJECTION -- key, description, and the
-- value flattened to readable text -- so searching for a word inside a list value finds
-- it without ever matching JSON punctuation.
CREATE VIRTUAL TABLE fields_fts USING fts5(text, tokenize='porter unicode61');
CREATE VIRTUAL TABLE notes_fts  USING fts5(text, tokenize='porter unicode61');

-- ...except for the one thing a store cannot reach: a CASCADE.
--
-- Deleting a persona removes its fields and notes through the foreign key above, and a
-- foreign key has no application code path to hook. Without these triggers the rows go
-- and every word of them stays in the index -- so a deleted persona would still be
-- findable by search, and the docid would later be handed to a different row.
--
-- That is not a contradiction of the "no triggers" rule above. That rule is about
-- *building* the index, which needs a JSON value rendered to readable text and
-- therefore belongs in Python where it can be unit-tested. This is only teardown: one
-- DELETE keyed on a column the row already carries, with nothing to render and nothing
-- to get subtly wrong.
--
-- Found by the index-integrity tests, which is what that test class is for.
CREATE TRIGGER fields_fts_after_delete AFTER DELETE ON fields BEGIN
    DELETE FROM fields_fts WHERE rowid = OLD.seq;
END;

CREATE TRIGGER notes_fts_after_delete AFTER DELETE ON notes BEGIN
    DELETE FROM notes_fts WHERE rowid = OLD.seq;
END;

-- The change log. No foreign keys, on purpose.
--
-- Deleting a persona must not delete the record that it was deleted. account_id and
-- profile are opaque and their rows may already be gone, so an entry has to be
-- self-describing: what it says is all that will be left.
--
-- sequence is the ordering, not `at`. The clock is injectable and two events recorded in
-- the same tick share a timestamp, so "newest first" has to be insertion order to be an
-- order at all.
--
-- subject is the field key or the note id. detail is a short human-readable summary and
-- NEVER holds a field value or a note body -- an event log is read by more people, and
-- kept for longer, than the rows it describes.
CREATE TABLE events (
    sequence    INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    NOT NULL UNIQUE,
    at          TEXT    NOT NULL,
    account_id  TEXT    NOT NULL,
    profile     TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    detail      TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    asserted_by TEXT    NOT NULL
) STRICT;

CREATE INDEX events_by_persona ON events(account_id, profile, sequence);

-- Partial indexes, each excluding forgotten rows.
--
-- Smaller than the full index, and the common query filters on that anyway. Each one
-- carries the sort columns as well as the filter columns, so it serves the ORDER BY too
-- and the query plan shows no temp B-tree -- which is what makes cursor pagination over
-- five thousand notes a seek rather than a sort.
--
-- Notes are ordered and range-filtered on created_at: a note is something that happened,
-- and when it happened is what you ask about. Fields are ordered and range-filtered on
-- updated_at: a field is current state, and when it last changed is what you ask about.
-- Each list endpoint's `since`/`until` therefore names the column its index is built on,
-- because a filter that cannot use the index is not a filter this service offers.
CREATE INDEX notes_live   ON notes(account_id, profile, created_at DESC, note_id DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX notes_pinned ON notes(account_id, profile, created_at DESC, note_id DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;
CREATE INDEX notes_kind   ON notes(account_id, profile, kind, created_at DESC, note_id DESC)
    WHERE forgotten_at IS NULL;

CREATE INDEX fields_live   ON fields(account_id, profile, updated_at DESC, key DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX fields_pinned ON fields(account_id, profile, updated_at DESC, key DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;
CREATE INDEX fields_source ON fields(account_id, profile, source, updated_at DESC, key DESC)
    WHERE forgotten_at IS NULL;

-- The include_forgotten path, which by definition cannot use any of the partial indexes
-- above. It gets an index that NARROWS but does not sort, and that shape is deliberate.
--
-- A full index on (account_id, profile, created_at DESC, note_id DESC) was tried first
-- and was a mistake: with no ANALYZE statistics the planner cannot tell a partial index
-- from a full one with the same prefix, so it picked the full one for *every* query and
-- the three partial indexes above became dead weight. Verified with EXPLAIN QUERY PLAN,
-- which is the only way anyone would ever find out.
--
-- Without the sort columns it cannot shadow them -- serving the ORDER BY is what those
-- are for -- and the rare listing that wants forgotten rows still reads only one
-- persona's notes instead of scanning the table across every account.
--
-- fields needs no equivalent: its PRIMARY KEY is (account_id, profile, key), so SQLite's
-- own autoindex already narrows that path to one persona.
CREATE INDEX notes_by_persona ON notes(account_id, profile);
