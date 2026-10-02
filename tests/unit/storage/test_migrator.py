"""Numbered SQL files, applied in order, recorded as they go."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.storage.migrator import MIGRATIONS_DIR, discover, migrate
from tests.fakes.clock import EPOCH

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from persona_api.storage.database import Database


@pytest.fixture
async def blank(tmp_path: Path) -> AsyncIterator[Database]:
    from persona_api.storage.database import Database as Db

    database = Db(tmp_path / "blank.db")
    try:
        yield database
    finally:
        await database.aclose()


class TestDiscovery:
    def test_it_finds_the_shipped_migrations(self) -> None:
        assert [migration.version for migration in discover()] == [1, 2]

    def test_it_orders_by_number_not_by_filename(self, tmp_path: Path) -> None:
        # Sorted as text, a tenth migration sorts between the first and the second --
        # and would then be applied against a schema two versions behind the one it was
        # written for.
        for name in ("0002_second.sql", "0010_tenth.sql", "0001_first.sql"):
            (tmp_path / name).write_text("SELECT 1;")

        assert [m.version for m in discover(tmp_path)] == [1, 2, 10]

    def test_a_migration_knows_its_own_filename(self, tmp_path: Path) -> None:
        (tmp_path / "0001_initial.sql").write_text("SELECT 1;")

        assert discover(tmp_path)[0].name == "0001_initial.sql"


class TestTheErasureUpgrade:
    async def test_what_was_already_forgotten_stays_kept_and_the_log_holds_no_values(
        self, blank: Database, tmp_path: Path
    ) -> None:
        """The bug, named: upgrading scheduled the destruction of rows forgotten before it.

        Every row forgotten before 0002 was a tombstone, and the person who forgot it
        never chose otherwise. The new column has to read as "never" for all of them.
        """
        first = tmp_path / "first"
        first.mkdir()
        (first / "0001_initial.sql").write_text(
            (MIGRATIONS_DIR / "0001_initial.sql").read_text(), encoding="utf-8"
        )
        migrate(blank, now=EPOCH, directory=first)
        await blank.execute(
            "INSERT INTO personas (account_id, profile, persona_id, created_at, updated_at)"
            " VALUES ('a', 'work', 'per_1', '2026-01-01', '2026-01-01')"
        )
        await blank.execute(
            "INSERT INTO fields (account_id, profile, key, field_id, seq, description,"
            " value_json, value_type, source, asserted_by, created_at, updated_at,"
            " forgotten_at) VALUES ('a', 'work', 'voice', 'fld_1', 1, 'd', '\"x\"',"
            " 'string', 'assistant', 'persona', '2026-01-01', '2026-01-01', '2026-01-02')"
        )
        await blank.execute(
            "INSERT INTO events (event_id, at, account_id, profile, action, subject, detail,"
            " source, asserted_by) VALUES ('evt_1', '2026-01-01', 'a', 'work',"
            " 'field.forgotten', 'voice', '', 'assistant', 'persona')"
        )

        assert migrate(blank, now=EPOCH) == 1

        field = await blank.fetch_one("SELECT forgotten_at, purge_after FROM fields")
        event = await blank.fetch_one("SELECT old_value FROM events")
        assert field is not None
        assert event is not None
        assert field["forgotten_at"] == "2026-01-02"
        assert field["purge_after"] is None
        assert event["old_value"] is None


class TestApplying:
    async def test_it_applies_everything_to_a_fresh_database(self, blank: Database) -> None:
        assert migrate(blank, now=EPOCH) == len(discover())

    async def test_it_is_idempotent(self, blank: Database) -> None:
        # Which is what makes it safe to call on every start, rather than something an
        # operator has to remember to run once.
        migrate(blank, now=EPOCH)

        assert migrate(blank, now=EPOCH) == 0

    async def test_it_records_what_it_applied_and_when(self, blank: Database) -> None:
        migrate(blank, now=EPOCH)

        rows = await blank.fetch_all("SELECT version, applied_at FROM schema_version")

        assert [row["version"] for row in rows] == [1, 2]
        assert rows[0]["applied_at"].startswith("2026-01-01")

    async def test_a_broken_script_leaves_the_schema_exactly_as_it_was(
        self, blank: Database, tmp_path: Path
    ) -> None:
        # SQLite has transactional DDL, so a migration that fails halfway must unwind
        # rather than leave half a schema with nothing to say so. Without the rollback
        # it would also hold a write lock on the way out.
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "0001_half.sql").write_text("CREATE TABLE bad (;")

        with pytest.raises(Exception, match="syntax error"):
            migrate(blank, now=EPOCH, directory=directory)

        # And the real migrations still apply afterwards, which is the proof that
        # nothing was left behind.
        assert migrate(blank, now=EPOCH) == len(discover())

    async def test_a_second_migration_runs_after_the_first(
        self, blank: Database, tmp_path: Path
    ) -> None:
        directory = tmp_path / "two"
        directory.mkdir()
        (directory / "0001_first.sql").write_text("CREATE TABLE a (x TEXT) STRICT;")
        (directory / "0002_second.sql").write_text("CREATE TABLE b (y TEXT) STRICT;")

        assert migrate(blank, now=EPOCH, directory=directory) == 2

        names = {
            row["name"]
            for row in await blank.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"a", "b"} <= names

    async def test_only_the_pending_ones_are_applied(self, blank: Database, tmp_path: Path) -> None:
        directory = tmp_path / "grow"
        directory.mkdir()
        (directory / "0001_first.sql").write_text("CREATE TABLE a (x TEXT) STRICT;")
        migrate(blank, now=EPOCH, directory=directory)

        (directory / "0002_second.sql").write_text("CREATE TABLE b (y TEXT) STRICT;")

        assert migrate(blank, now=EPOCH, directory=directory) == 1


class TestTheShippedSchema:
    async def test_the_migrations_directory_is_where_the_migrator_looks(self) -> None:
        assert (MIGRATIONS_DIR / "0001_initial.sql").exists()
