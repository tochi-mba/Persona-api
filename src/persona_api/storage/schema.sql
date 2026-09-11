CREATE INDEX events_by_persona ON events(account_id, profile, sequence);
CREATE INDEX fields_live   ON fields(account_id, profile, updated_at DESC, key DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX fields_pinned ON fields(account_id, profile, updated_at DESC, key DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;
CREATE INDEX fields_source ON fields(account_id, profile, source, updated_at DESC, key DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX notes_by_persona ON notes(account_id, profile);
CREATE INDEX notes_kind   ON notes(account_id, profile, kind, created_at DESC, note_id DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX notes_live   ON notes(account_id, profile, created_at DESC, note_id DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX notes_pinned ON notes(account_id, profile, created_at DESC, note_id DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;
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
CREATE VIRTUAL TABLE fields_fts USING fts5(text, tokenize='porter unicode61');
CREATE TABLE 'fields_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE 'fields_fts_content'(id INTEGER PRIMARY KEY, c0);
CREATE TABLE 'fields_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE 'fields_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'fields_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE fts_sequence (
    name TEXT    NOT NULL PRIMARY KEY,
    next INTEGER NOT NULL
) STRICT;
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
CREATE VIRTUAL TABLE notes_fts  USING fts5(text, tokenize='porter unicode61');
CREATE TABLE 'notes_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE 'notes_fts_content'(id INTEGER PRIMARY KEY, c0);
CREATE TABLE 'notes_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE 'notes_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'notes_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
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
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL
) STRICT
;
CREATE TABLE sqlite_sequence(name,seq);
