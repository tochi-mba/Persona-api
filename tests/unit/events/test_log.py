"""The change log: what it records, what it refuses to record, and how it stays bounded."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.provenance import Source
from persona_api.events.log import Event, EventAction, EventLog
from persona_api.events.sql_log import SqlEventLog
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.fakes.clock import EPOCH, FakeClock

if TYPE_CHECKING:
    from persona_api.storage.database import Database


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def log(database: Database, clock: FakeClock) -> SqlEventLog:
    return SqlEventLog(database=database, clock=clock)


async def persona(database: Database, account_id: str = ACCOUNT, profile: str = PROFILE) -> None:
    """A persona for events to be about. Events have no foreign key to it, deliberately."""
    await database.execute(
        "INSERT INTO personas (account_id, profile, persona_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (account_id, profile, f"per_{account_id}_{profile}", "2026-01-01", "2026-01-01"),
    )


async def write(
    log: SqlEventLog,
    *,
    action: EventAction = EventAction.FIELD_SET,
    account_id: str = ACCOUNT,
    profile: str = PROFILE,
    subject: str = "voice",
    detail: str = "",
) -> Event:
    return await log.record(
        action=action,
        account_id=account_id,
        profile=profile,
        subject=subject,
        detail=detail,
        source=Source.ASSISTANT,
        asserted_by="persona",
    )


class TestThePort:
    def test_the_adapter_satisfies_it(self, log: SqlEventLog) -> None:
        checked: EventLog = log

        assert isinstance(checked, EventLog)


class TestRecording:
    async def test_an_event_comes_back_as_it_was_written(self, log: SqlEventLog) -> None:
        recorded = await write(log, detail="value changed")

        read, _ = await log.recent(ACCOUNT, PROFILE)

        assert read == [recorded]

    async def test_it_carries_both_halves_of_the_provenance(self, log: SqlEventLog) -> None:
        # source is the claim, asserted_by is server-derived. An event is a write like
        # any other, so it records the same pair a field does.
        recorded = await write(log)

        assert recorded.source is Source.ASSISTANT
        assert recorded.asserted_by == "persona"

    async def test_the_timestamp_comes_from_the_injected_clock(
        self, log: SqlEventLog, clock: FakeClock
    ) -> None:
        clock.advance(3600)

        recorded = await write(log)

        assert (recorded.at - EPOCH).total_seconds() == 3600


class TestOrdering:
    async def test_events_read_newest_first(self, log: SqlEventLog) -> None:
        for key in ("first", "second", "third"):
            await write(log, subject=key)

        read, _ = await log.recent(ACCOUNT, PROFILE)

        assert [event.subject for event in read] == ["third", "second", "first"]

    async def test_two_events_in_one_tick_still_have_an_order(self, log: SqlEventLog) -> None:
        # The clock is injectable, so events recorded in the same tick share a
        # timestamp. If "newest first" sorted on `at`, this would be an approximation
        # of an order rather than an order -- which is why it sorts on sequence.
        first = await write(log, subject="first")
        second = await write(log, subject="second")

        assert first.at == second.at
        assert second.sequence > first.sequence

        read, _ = await log.recent(ACCOUNT, PROFILE)
        assert [event.subject for event in read] == ["second", "first"]


class TestIsolation:
    async def test_one_accounts_log_never_shows_anothers_event(self, log: SqlEventLog) -> None:
        await write(log, account_id=ACCOUNT, subject="mine")
        await write(log, account_id=OTHER_ACCOUNT, subject="theirs")

        read, _ = await log.recent(ACCOUNT, PROFILE)

        assert [event.subject for event in read] == ["mine"]

    async def test_one_profiles_log_never_shows_anothers_event(self, log: SqlEventLog) -> None:
        # Two assistants for one person are different people, so their histories are
        # separate for the same reason their personas are.
        await write(log, profile="work", subject="at work")
        await write(log, profile="home", subject="at home")

        read, _ = await log.recent(ACCOUNT, "work")

        assert [event.subject for event in read] == ["at work"]

    async def test_an_account_with_no_events_reads_empty_rather_than_erroring(
        self, log: SqlEventLog
    ) -> None:
        # Identical to the answer for a persona that exists and has never changed.
        # There is nothing here that distinguishes "not yours" from "not there".
        read, following = await log.recent("acct_nobody", PROFILE)

        assert read == []
        assert following is None


class TestPaging:
    async def test_it_walks_every_event_exactly_once(self, log: SqlEventLog) -> None:
        for index in range(25):
            await write(log, subject=f"key{index:02d}")

        seen: list[str] = []
        cursor: str | None = None
        while True:
            page, cursor = await log.recent(ACCOUNT, PROFILE, limit=10, cursor=cursor)
            seen.extend(event.subject for event in page)
            if cursor is None:
                break

        assert seen == [f"key{index:02d}" for index in reversed(range(25))]

    async def test_a_write_during_a_walk_cannot_duplicate_or_skip_a_row(
        self, log: SqlEventLog
    ) -> None:
        # The cursor names the last sequence seen, so a row appended above the walk is
        # simply not in it. An offset would shift and show one event twice.
        for index in range(20):
            await write(log, subject=f"key{index:02d}")

        first, cursor = await log.recent(ACCOUNT, PROFILE, limit=10)
        await write(log, subject="arrived-mid-walk")
        second, _ = await log.recent(ACCOUNT, PROFILE, limit=10, cursor=cursor)

        subjects = [event.subject for event in first + second]
        assert len(subjects) == len(set(subjects))
        assert "arrived-mid-walk" not in subjects

    async def test_the_last_page_reports_no_cursor(self, log: SqlEventLog) -> None:
        for index in range(3):
            await write(log, subject=f"key{index}")

        _, cursor = await log.recent(ACCOUNT, PROFILE, limit=10)

        assert cursor is None

    async def test_a_full_page_with_nothing_after_it_reports_no_cursor(
        self, log: SqlEventLog
    ) -> None:
        # The off-by-one that a "return a cursor whenever the page is full" rule gets
        # wrong: the caller then makes one more request for an empty page every time.
        for index in range(10):
            await write(log, subject=f"key{index}")

        _, cursor = await log.recent(ACCOUNT, PROFILE, limit=10)

        assert cursor is None


class TestTheCap:
    async def test_the_oldest_entries_go_when_the_bound_is_reached(
        self, database: Database, clock: FakeClock
    ) -> None:
        log = SqlEventLog(database=database, clock=clock, max_entries=5)

        for index in range(8):
            await write(log, subject=f"key{index}")

        read, _ = await log.recent(ACCOUNT, PROFILE, limit=100)

        assert [event.subject for event in read] == [f"key{index}" for index in (7, 6, 5, 4, 3)]

    async def test_the_bound_holds_under_concurrent_writes(
        self, database: Database, clock: FakeClock
    ) -> None:
        # Trimmed inside the insert's own transaction, so the bound is a bound rather
        # than something a sweeper gets round to. A trim done outside the transaction
        # is one that two concurrent writers both skip.
        log = SqlEventLog(database=database, clock=clock, max_entries=10)

        await asyncio.gather(*(write(log, subject=f"key{index}") for index in range(40)))

        assert await log.count() == 10

    async def test_counting_spans_every_account_because_it_is_for_health(
        self, log: SqlEventLog
    ) -> None:
        await write(log, account_id=ACCOUNT)
        await write(log, account_id=OTHER_ACCOUNT)

        assert await log.count() == 2


class TestSurvivingTheRowsItDescribes:
    async def test_deleting_a_persona_does_not_delete_the_record_that_it_was_deleted(
        self, log: SqlEventLog, database: Database
    ) -> None:
        # The reason this table has no foreign keys. An entry has to be
        # self-describing, because what it says is all that will be left.
        await persona(database)
        await write(log, action=EventAction.PERSONA_DELETED, subject=PROFILE)

        await database.execute(
            "DELETE FROM personas WHERE account_id = ? AND profile = ?", (ACCOUNT, PROFILE)
        )

        read, _ = await log.recent(ACCOUNT, PROFILE)
        assert [event.action for event in read] == [EventAction.PERSONA_DELETED]


class TestWhatAnEventMayNotContain:
    async def test_the_actions_are_a_closed_set(self) -> None:
        # A typo'd free string is an event that can never be filtered for again.
        assert {action.value for action in EventAction} == {
            "persona.created",
            "persona.updated",
            "persona.deleted",
            "field.set",
            "field.revised",
            "field.forgotten",
            "note.written",
            "note.revised",
            "note.forgotten",
        }

    async def test_every_action_is_a_noun_dot_verb(self) -> None:
        # So a prefix match answers "everything that happened to fields".
        assert all(action.value.count(".") == 1 for action in EventAction)

    async def test_an_event_has_no_field_for_a_value_or_a_body(self) -> None:
        # The structural half of the rule. An event log is read more often, by more
        # tools, and kept for longer than the rows it describes -- and it is the one
        # table here with no tombstone, so anything written into it cannot be forgotten.
        fields = set(Event.__dataclass_fields__)

        assert "value" not in fields
        assert "value_json" not in fields
        assert "body" not in fields
