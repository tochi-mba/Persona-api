"""The built schema, and the query plans the index shapes exist to produce.

Two kinds of assertion live here and both catch things that fail silently.

The **snapshot** catches a migration and a row-mapper diverging, which is the
characteristic failure of hand-rolled migrations: the DDL changes, the mapper does not,
and nothing notices until a column reads back as the wrong type.

The **query plans** catch an index that has stopped being used. That is worse than it
sounds, because nothing breaks -- the query still returns the right rows, just by
scanning and sorting. A full index on notes was added during the build for the
include_forgotten path and silently shadowed all three partial indexes, because with no
ANALYZE statistics the planner cannot tell a partial index from a full one sharing its
prefix. These assertions are what found it and what stops it coming back.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from persona_api.storage.migrator import MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona_api.storage.database import Database

SNAPSHOT = MIGRATIONS_DIR.parent / "schema.sql"

STAMP = "2026-01-01T12:00:00.000000+00:00"


async def dump_schema(database: Database) -> str:
    """Every object in the database, in a stable order."""
    rows = await database.fetch_all(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
    )
    return "".join(f"{row['sql']};\n" for row in rows)


@pytest.fixture
def schema() -> Iterator[sqlite3.Connection]:
    """The schema on a throwaway in-memory connection, for plan inspection."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript((MIGRATIONS_DIR / "0001_initial.sql").read_text())
    try:
        yield connection
    finally:
        connection.close()


def plan(connection: sqlite3.Connection, sql: str, *parameters: object) -> str:
    return " | ".join(
        row["detail"] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
    )


class TestTheSnapshot:
    async def test_it_matches_the_checked_in_copy(self, database: Database) -> None:
        # When this fails, either a migration changed and `make schema` needs running
        # -- and the diff is the review of what the migration actually did -- or
        # something changed the schema that did not mean to.
        assert await dump_schema(database) == SNAPSHOT.read_text()


class TestTheLiveQueryPlans:
    """Every path a list endpoint takes, as a seek with no sort."""

    @pytest.mark.parametrize(
        ("index", "sql", "parameters"),
        [
            (
                "notes_live",
                "SELECT * FROM notes WHERE account_id=? AND profile=? AND forgotten_at IS NULL "
                "ORDER BY created_at DESC, note_id DESC LIMIT 20",
                ("a", "work"),
            ),
            (
                "notes_pinned",
                "SELECT * FROM notes WHERE account_id=? AND profile=? AND pinned=1 "
                "AND forgotten_at IS NULL ORDER BY created_at DESC, note_id DESC LIMIT 20",
                ("a", "work"),
            ),
            (
                "notes_kind",
                "SELECT * FROM notes WHERE account_id=? AND profile=? AND kind=? "
                "AND forgotten_at IS NULL ORDER BY created_at DESC, note_id DESC LIMIT 20",
                ("a", "work", "lesson"),
            ),
            (
                "fields_live",
                "SELECT * FROM fields WHERE account_id=? AND profile=? AND forgotten_at IS NULL "
                "ORDER BY updated_at DESC, key DESC LIMIT 20",
                ("a", "work"),
            ),
            (
                "fields_pinned",
                "SELECT * FROM fields WHERE account_id=? AND profile=? AND pinned=1 "
                "AND forgotten_at IS NULL ORDER BY updated_at DESC, key DESC LIMIT 20",
                ("a", "work"),
            ),
            (
                "fields_source",
                "SELECT * FROM fields WHERE account_id=? AND profile=? AND source=? "
                "AND forgotten_at IS NULL ORDER BY updated_at DESC, key DESC LIMIT 20",
                ("a", "work", "owner"),
            ),
        ],
    )
    def test_the_partial_index_serves_both_the_filter_and_the_sort(
        self, schema: sqlite3.Connection, index: str, sql: str, parameters: tuple[object, ...]
    ) -> None:
        chosen = plan(schema, sql, *parameters)

        assert index in chosen, chosen
        # No temp B-tree: the index carries the sort columns, so the ORDER BY is a walk
        # rather than a sort. On a persona with five thousand notes that is the
        # difference between a seek and reading the lot.
        assert "TEMP B-TREE" not in chosen, chosen

    def test_a_cursor_page_is_a_seek_rather_than_a_sort(self, schema: sqlite3.Connection) -> None:
        # The row-value comparison is what makes keyset pagination one index seek. An
        # offset would be correct too, and would shift under a concurrent write.
        chosen = plan(
            schema,
            "SELECT * FROM notes WHERE account_id=? AND profile=? AND forgotten_at IS NULL "
            "AND (created_at, note_id) < (?, ?) ORDER BY created_at DESC, note_id DESC LIMIT 20",
            "a",
            "work",
            STAMP,
            "note_x",
        )

        assert "notes_live" in chosen, chosen
        assert "TEMP B-TREE" not in chosen, chosen

    def test_a_since_filter_is_a_range_seek_on_the_same_index(
        self, schema: sqlite3.Connection
    ) -> None:
        chosen = plan(
            schema,
            "SELECT * FROM notes WHERE account_id=? AND profile=? AND forgotten_at IS NULL "
            "AND created_at >= ? ORDER BY created_at DESC, note_id DESC LIMIT 20",
            "a",
            "work",
            STAMP,
        )

        assert "notes_live" in chosen, chosen
        assert "TEMP B-TREE" not in chosen, chosen

    def test_a_key_prefix_search_uses_the_fields_primary_key(
        self, schema: sqlite3.Connection
    ) -> None:
        chosen = plan(
            schema,
            "SELECT * FROM fields WHERE account_id=? AND profile=? AND key >= ? AND key < ?",
            "a",
            "work",
            "vo",
            "vp",
        )

        assert "SEARCH fields" in chosen, chosen
        assert "SCAN" not in chosen, chosen

    def test_the_batch_key_fetch_uses_the_fields_primary_key(
        self, schema: sqlite3.Connection
    ) -> None:
        # ?keys=voice,tone exists so an assistant makes one call rather than thirty.
        chosen = plan(
            schema,
            "SELECT * FROM fields WHERE account_id=? AND profile=? AND key IN (?,?)",
            "a",
            "work",
            "voice",
            "tone",
        )

        assert "SEARCH fields" in chosen, chosen
        assert "SCAN" not in chosen, chosen

    def test_reading_the_event_log_is_a_seek(self, schema: sqlite3.Connection) -> None:
        chosen = plan(
            schema,
            "SELECT * FROM events WHERE account_id=? AND profile=? ORDER BY sequence DESC LIMIT 50",
            "a",
            "work",
        )

        assert "events_by_persona" in chosen, chosen
        assert "TEMP B-TREE" not in chosen, chosen


class TestTheIncludeForgottenPath:
    def test_it_narrows_to_one_persona_rather_than_scanning_every_account(
        self, schema: sqlite3.Connection
    ) -> None:
        # It cannot use a partial index, by definition. What it must not do is read
        # every account's notes -- so it gets an index that narrows without sorting.
        chosen = plan(
            schema,
            "SELECT * FROM notes WHERE account_id=? AND profile=? "
            "ORDER BY created_at DESC, note_id DESC LIMIT 20",
            "a",
            "work",
        )

        assert "SEARCH notes" in chosen, chosen
        assert "SCAN notes" not in chosen, chosen

    def test_the_narrowing_index_carries_no_sort_columns(self) -> None:
        # This is the assertion that keeps the bug from coming back. Give this index
        # the sort columns and the planner prefers it for EVERY query -- it cannot tell
        # a partial index from a full one sharing its prefix without ANALYZE -- and all
        # three partial indexes above become dead weight, silently.
        sql = (MIGRATIONS_DIR / "0001_initial.sql").read_text()

        assert "CREATE INDEX notes_by_persona ON notes(account_id, profile);" in sql


class TestTheFtsDocidAllocator:
    def test_it_hands_out_strictly_increasing_ids(self, schema: sqlite3.Connection) -> None:
        allocated = [
            schema.execute(
                "UPDATE fts_sequence SET next = next + 1 WHERE name='notes' RETURNING next"
            ).fetchone()["next"]
            for _ in range(5)
        ]

        assert allocated == [1, 2, 3, 4, 5]

    def test_the_two_indexes_have_separate_counters(self, schema: sqlite3.Connection) -> None:
        schema.execute("UPDATE fts_sequence SET next = next + 1 WHERE name='notes'")
        row = schema.execute("SELECT next FROM fts_sequence WHERE name='fields'").fetchone()

        assert row["next"] == 0

    def test_a_freed_id_is_never_reused(self, schema: sqlite3.Connection) -> None:
        # A counter that only goes up means a stale index entry can never be mistaken
        # for a live one -- and it is why the docid is not the row's implicit rowid,
        # which VACUUM renumbers.
        first = schema.execute(
            "UPDATE fts_sequence SET next = next + 1 WHERE name='notes' RETURNING next"
        ).fetchone()["next"]
        second = schema.execute(
            "UPDATE fts_sequence SET next = next + 1 WHERE name='notes' RETURNING next"
        ).fetchone()["next"]

        assert second > first


class TestTheCascade:
    def test_deleting_a_persona_takes_its_fields_and_notes(
        self, schema: sqlite3.Connection
    ) -> None:
        # Through the foreign key rather than a hand-ordered sequence of deletes, so a
        # failure partway cannot leave rows nothing can reach, read or delete.
        schema.execute(
            "INSERT INTO personas (account_id, profile, persona_id, created_at, updated_at)"
            " VALUES ('a', 'work', 'per_1', ?, ?)",
            (STAMP, STAMP),
        )
        schema.execute(
            "INSERT INTO fields (account_id, profile, key, field_id, seq, description,"
            " value_json, value_type, source, asserted_by, created_at, updated_at)"
            " VALUES ('a','work','voice','fld_1',1,'how it speaks','\"dry\"','string',"
            "'assistant','persona',?,?)",
            (STAMP, STAMP),
        )
        schema.execute(
            "INSERT INTO notes (note_id, account_id, profile, seq, body, kind, source,"
            " asserted_by, created_at, updated_at)"
            " VALUES ('note_1','a','work',1,'they went quiet','episode','assistant',"
            "'persona',?,?)",
            (STAMP, STAMP),
        )

        schema.execute("DELETE FROM personas WHERE account_id='a' AND profile='work'")

        assert schema.execute("SELECT count(*) AS n FROM fields").fetchone()["n"] == 0
        assert schema.execute("SELECT count(*) AS n FROM notes").fetchone()["n"] == 0

    def test_the_event_log_survives_the_persona_it_describes(
        self, schema: sqlite3.Connection
    ) -> None:
        # No foreign keys on events, on purpose: deleting a persona must not delete the
        # record that it was deleted.
        schema.execute(
            "INSERT INTO personas (account_id, profile, persona_id, created_at, updated_at)"
            " VALUES ('a', 'work', 'per_1', ?, ?)",
            (STAMP, STAMP),
        )
        schema.execute(
            "INSERT INTO events (event_id, at, account_id, profile, action, subject,"
            " detail, source, asserted_by)"
            " VALUES ('evt_1', ?, 'a', 'work', 'persona.deleted', 'work', '', "
            "'assistant', 'persona')",
            (STAMP,),
        )

        schema.execute("DELETE FROM personas WHERE account_id='a' AND profile='work'")

        assert schema.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 1


class TestStrictness:
    def test_every_table_is_strict(self, schema: sqlite3.Connection) -> None:
        # Without STRICT, SQLite stores a string in an INTEGER column happily, and a
        # row-mapper bug becomes data that reads back as the wrong type months later
        # rather than a failure at the write.
        rows = schema.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()
        # The FTS5 shadow tables and sqlite_sequence are SQLite's own, created by the
        # virtual table and by AUTOINCREMENT respectively. We do not write their DDL
        # and cannot make them STRICT.
        ordinary = [
            row
            for row in rows
            if not row["name"].startswith(("fields_fts", "notes_fts", "sqlite_"))
        ]

        assert ordinary
        assert all("STRICT" in row["sql"] for row in ordinary), [
            row["name"] for row in ordinary if "STRICT" not in row["sql"]
        ]
