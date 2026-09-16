"""The one connection, and the three things about it that are load-bearing."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest

from persona_api.storage.database import (
    DATABASE_FILE_MODE,
    SIDECARS,
    Database,
    StorageError,
    make_private,
    require_foreign_keys,
)
from tests.support.filemode import assert_mode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "persona.db")
    try:
        yield database
    finally:
        await database.aclose()


class TestTheFileMode:
    """The only thing protecting a persona from every other process on the box.

    keyring encrypts its credential material, so a world-readable file there leaks
    metadata. Nothing in this database is encrypted: a persona is a person's own words
    about themselves, in plaintext, and 0644 would hand them to anybody with a shell.
    """

    async def test_the_database_is_owner_only(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "persona.db")
        try:
            path = tmp_path / "persona.db"
        finally:
            await database.aclose()

        assert_mode(path, DATABASE_FILE_MODE)

    async def test_the_write_ahead_log_is_owner_only_too(self, tmp_path: Path) -> None:
        # The WAL holds the same data the database does. A 0600 database beside a 0644
        # journal is a 0644 database with extra steps.
        database = Database(tmp_path / "persona.db")
        try:
            await database.execute("CREATE TABLE t (a TEXT)")
            await database.execute("INSERT INTO t (a) VALUES ('x')")
            sidecars = [
                (tmp_path / f"persona.db{suffix}")
                for suffix in SIDECARS
                if (tmp_path / f"persona.db{suffix}").exists()
            ]
            assert sidecars, "the WAL should exist after a write"
            for path in sidecars:
                assert_mode(path, DATABASE_FILE_MODE)
        finally:
            await database.aclose()

    def test_a_sidecar_that_does_not_exist_yet_is_skipped(self, tmp_path: Path) -> None:
        # make_private runs during connect, before the first write, so on a brand new
        # database the -wal and -shm files may not exist yet. Reaching for them would
        # crash the open path -- and the file whose mode actually matters is the one
        # that is there.
        lonely = tmp_path / "lonely.db"
        lonely.touch(mode=0o644)

        make_private(lonely)

        assert_mode(lonely, DATABASE_FILE_MODE)
        assert not (tmp_path / "lonely.db-wal").exists()

    def test_a_sidecar_left_behind_by_an_unclean_shutdown_is_made_private(
        self, tmp_path: Path
    ) -> None:
        # SQLite gives a WAL the permissions of the database it belongs to, so on a
        # first open there is nothing to fix. On a REOPEN there can be: a process
        # killed mid-transaction leaves its -wal and -shm behind, and those hold the
        # same plaintext the database does. This is the branch that covers them.
        existing = tmp_path / "reopened.db"
        existing.touch(mode=0o600)
        for suffix in SIDECARS:
            (tmp_path / f"reopened.db{suffix}").touch(mode=0o644)

        make_private(existing)

        for suffix in SIDECARS:
            assert_mode(tmp_path / f"reopened.db{suffix}", DATABASE_FILE_MODE)

    async def test_the_parent_directory_is_created_private(self, tmp_path: Path) -> None:
        nested = tmp_path / "var" / "persona.db"
        database = Database(nested)
        try:
            parent = nested.parent
        finally:
            await database.aclose()

        # 0700, because a world-readable directory leaks the fact that a persona
        # database exists here at all.
        assert_mode(parent, 0o700)


class TestForeignKeys:
    async def test_they_are_actually_on(self, db: Database) -> None:
        # PRAGMA foreign_keys defaults off, is per-connection, and is a silent no-op
        # while a transaction is open. Here the consequence of it quietly failing is
        # that deleting a persona leaves its fields and notes behind -- orphaned rows
        # nothing can reach, read or delete, holding exactly the text somebody asked to
        # be rid of.
        (enabled,) = await db.fetch_one("PRAGMA foreign_keys") or (0,)

        assert enabled

    def test_a_connection_without_them_is_refused(self) -> None:
        # Read back rather than trusted, which is the whole point.
        naked = sqlite3.connect(":memory:")
        try:
            naked.execute("PRAGMA foreign_keys = OFF")
            with pytest.raises(StorageError, match="foreign keys"):
                require_foreign_keys(naked)
        finally:
            naked.close()

    def test_a_connection_with_them_passes(self) -> None:
        naked = sqlite3.connect(":memory:", isolation_level=None)
        try:
            naked.execute("PRAGMA foreign_keys = ON")
            require_foreign_keys(naked)
        finally:
            naked.close()


class TestPragmas:
    async def test_it_runs_in_write_ahead_logging_mode(self, db: Database) -> None:
        row = await db.fetch_one("PRAGMA journal_mode")

        assert row is not None
        assert row[0] == "wal"

    async def test_synchronous_is_normal_rather_than_full(self, db: Database) -> None:
        # The one pragma that differs from keyring's. A lost commit there is a
        # credential somebody believes is saved; here it is a forgotten note, and the
        # write volume is orders of magnitude higher. WAL with NORMAL cannot corrupt,
        # only lose the last commits. See docs/adr/0008-sqlite.md.
        row = await db.fetch_one("PRAGMA synchronous")

        assert row is not None
        assert row[0] == 1


class TestTransactions:
    async def test_work_that_succeeds_is_committed(self, db: Database) -> None:
        await db.execute("CREATE TABLE t (a TEXT)")

        await db.transact(lambda c: c.execute("INSERT INTO t (a) VALUES ('kept')"))

        assert await db.count("SELECT count(*) AS total FROM t") == 1

    async def test_work_that_raises_is_rolled_back_whole(self, db: Database) -> None:
        # Including a domain error refusing the write, which is how every cap in this
        # service is enforced: the check and the write are one transaction, so a
        # refusal un-does the part that already ran.
        await db.execute("CREATE TABLE t (a TEXT)")

        def half_a_write(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO t (a) VALUES ('doomed')")
            msg = "the cap would be exceeded"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="cap"):
            await db.transact(half_a_write)

        assert await db.count("SELECT count(*) AS total FROM t") == 0


class TestReads:
    async def test_fetch_one_returns_none_when_there_is_no_row(self, db: Database) -> None:
        await db.execute("CREATE TABLE t (a TEXT)")

        assert await db.fetch_one("SELECT a FROM t") is None

    async def test_fetch_all_returns_every_row(self, db: Database) -> None:
        await db.execute("CREATE TABLE t (a TEXT)")
        await db.execute("INSERT INTO t (a) VALUES ('one'), ('two')")

        assert [row["a"] for row in await db.fetch_all("SELECT a FROM t")] == ["one", "two"]

    async def test_count_reads_the_aggregate(self, db: Database) -> None:
        await db.execute("CREATE TABLE t (a TEXT)")
        await db.execute("INSERT INTO t (a) VALUES ('one')")

        assert await db.count("SELECT count(*) AS total FROM t") == 1

    async def test_execute_reports_how_many_rows_it_touched(self, db: Database) -> None:
        await db.execute("CREATE TABLE t (a TEXT)")
        await db.execute("INSERT INTO t (a) VALUES ('one'), ('two')")

        assert await db.execute("DELETE FROM t") == 2

    async def test_run_sync_works_before_there_is_an_event_loop(self, tmp_path: Path) -> None:
        # Startup path: opening and migrating happen before there is a loop to keep
        # responsive, and the composition root is synchronous.
        database = Database(tmp_path / "persona.db")
        try:
            result = database.run_sync(lambda c: c.execute("SELECT 1").fetchone()[0])
        finally:
            await database.aclose()

        assert result == 1

    async def test_the_path_is_readable_for_diagnostics(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "persona.db")
        try:
            assert database.path == tmp_path / "persona.db"
        finally:
            await database.aclose()


class TestClosing:
    async def test_closing_twice_is_safe(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "persona.db")

        await database.aclose()
        await database.aclose()


class TestSerialization:
    async def test_concurrent_writes_all_land(self, db: Database) -> None:
        # Every transaction is one submitted callable on one worker thread, so there is
        # no point inside a transaction at which another caller can interleave. The
        # assertion is the OUTCOME -- twenty rows -- rather than the mechanism, so it
        # would still mean something if the single connection were replaced by a pool.
        await db.execute("CREATE TABLE t (a INTEGER)")

        def insert(value: int) -> Callable[[sqlite3.Connection], sqlite3.Cursor]:
            return lambda connection: connection.execute("INSERT INTO t (a) VALUES (?)", (value,))

        await asyncio.gather(*(db.transact(insert(index)) for index in range(20)))

        assert await db.count("SELECT count(*) AS total FROM t") == 20

    async def test_a_read_modify_write_cannot_be_interleaved(self, db: Database) -> None:
        # The property every cap in this service depends on: a count taken inside a
        # transaction is still true when that same transaction writes.
        await db.execute("CREATE TABLE counter (n INTEGER)")
        await db.execute("INSERT INTO counter (n) VALUES (0)")

        def increment(connection: sqlite3.Connection) -> None:
            current = connection.execute("SELECT n FROM counter").fetchone()["n"]
            connection.execute("UPDATE counter SET n = ?", (current + 1,))

        await asyncio.gather(*(db.transact(increment) for _ in range(50)))

        row = await db.fetch_one("SELECT n FROM counter")
        assert row is not None
        assert row["n"] == 50
