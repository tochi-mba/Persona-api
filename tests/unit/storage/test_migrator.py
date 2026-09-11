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
        assert [migration.version for migration in discover()] == [1]

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

        assert [row["version"] for row in rows] == [1]
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
