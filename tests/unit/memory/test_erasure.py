"""Forgetting as the person chose it, and the values the log kept, at the store.

Three modes and one log setting, and the properties the catalogue promises for them: a
person who chose nothing sees exactly what persona-api always did, a change of mind is
never retroactive, and whatever destroys a row destroys the copies the log kept of it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.erasure import TOMBSTONE, ErasureMode, ErasurePolicy
from persona_api.domain.errors import FieldNotFoundError, NoteNotFoundError
from persona_api.domain.provenance import Source
from persona_api.events.log import EventAction
from persona_api.memory.sweeper import Sweeper
from persona_api.personas.store import PersonaStore
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, set_field, write_note

if TYPE_CHECKING:
    from persona_api.events.sql_log import SqlEventLog
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.storage.database import Database
    from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.usefixtures("personas")

GRACE = ErasurePolicy(mode=ErasureMode.GRACE, grace_days=7)
IMMEDIATE = ErasurePolicy(mode=ErasureMode.IMMEDIATE)


async def forget_field(
    fields: FieldStore,
    key: str = "voice",
    *,
    erasure: ErasurePolicy = TOMBSTONE,
    account_id: str = ACCOUNT,
) -> None:
    await fields.forget(
        account_id,
        PROFILE,
        key,
        source=Source.ASSISTANT,
        asserted_by=ASSERTED_BY,
        erasure=erasure,
    )


async def forget_note(
    notes: NoteStore, note_id: str, *, erasure: ErasurePolicy = TOMBSTONE
) -> None:
    await notes.forget(
        ACCOUNT, PROFILE, note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY, erasure=erasure
    )


async def rows(database: Database, table: str) -> int:
    return await database.count(f"SELECT count(*) AS total FROM {table}")  # noqa: S608


class TestATombstone:
    async def test_forgetting_with_no_choice_keeps_the_row_for_ever(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        """The bug, named: forgetting destroyed a row for somebody who never chose erasure."""
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )
        clock.advance(timedelta(days=3650))

        assert await fields.purge_due(now=clock.now(), limit=100) == 0
        kept = await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True)
        assert kept is not None
        assert kept.purge_after is None

    async def test_its_event_says_nothing_new(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)
        await forget_field(fields)

        latest, _ = await events.recent(ACCOUNT, PROFILE, limit=1)
        assert latest[0].action is EventAction.FIELD_FORGOTTEN
        assert latest[0].detail == ""


class TestAGracePeriod:
    async def test_the_row_says_when_it_will_go(
        self, fields: FieldStore, notes: NoteStore, clock: FakeClock
    ) -> None:
        await set_field(fields)
        note = await write_note(notes)

        await forget_field(fields, erasure=GRACE)
        await forget_note(notes, note.note_id, erasure=GRACE)

        field = await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True)
        kept = await notes.get(ACCOUNT, PROFILE, note.note_id, include_forgotten=True)
        assert field is not None
        assert kept is not None
        assert field.purge_after == clock.now() + timedelta(days=7)
        assert kept.purge_after == clock.now() + timedelta(days=7)

    async def test_it_survives_until_the_window_has_passed(
        self, fields: FieldStore, notes: NoteStore, clock: FakeClock
    ) -> None:
        await set_field(fields)
        note = await write_note(notes)
        await forget_field(fields, erasure=GRACE)
        await forget_note(notes, note.note_id, erasure=GRACE)

        clock.advance(timedelta(days=7) - timedelta(seconds=1))
        assert await fields.purge_due(now=clock.now(), limit=100) == 0
        assert await notes.purge_due(now=clock.now(), limit=100) == 0

        clock.advance(timedelta(seconds=1))
        assert await fields.purge_due(now=clock.now(), limit=100) == 1
        assert await notes.purge_due(now=clock.now(), limit=100) == 1
        assert await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True) is None
        assert await notes.get(ACCOUNT, PROFILE, note.note_id, include_forgotten=True) is None

    async def test_setting_the_field_again_calls_the_destruction_off(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        """The bug, named: a field revived inside its grace period was still destroyed later."""
        await set_field(fields, value="dry")
        await forget_field(fields, erasure=GRACE)
        revived = await set_field(fields, value="dry")

        clock.advance(timedelta(days=30))

        assert revived.purge_after is None
        assert await fields.purge_due(now=clock.now(), limit=100) == 0
        assert await fields.get(ACCOUNT, PROFILE, "voice") is not None

    async def test_a_key_set_again_after_its_window_but_before_the_sweep_starts_afresh(
        self, fields: FieldStore, events: SqlEventLog, clock: FakeClock
    ) -> None:
        """The bug, named: a write after the grace period, before the sweep, revived the row.

        The person chose to have it gone by then. Reviving would bring back its revision
        history and the old values the log kept, which only the sweep's timing had spared.
        """
        await set_field(fields, value="first")
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="how it speaks",
            value="second",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            log_values=True,
        )
        await forget_field(fields, erasure=GRACE)
        clock.advance(timedelta(days=7))

        again = await set_field(fields, value="third")

        assert again.revision == 1
        assert again.purge_after is None
        logged, _ = await events.recent(ACCOUNT, PROFILE)
        assert all(event.old_value is None for event in logged)
        assert await fields.purge_due(now=clock.now(), limit=100) == 0

    async def test_a_key_set_again_after_destruction_is_a_new_field(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields)
        await forget_field(fields, erasure=GRACE)
        clock.advance(timedelta(days=8))
        await fields.purge_due(now=clock.now(), limit=100)

        again = await set_field(fields, value="new")

        assert again.revision == 1
        assert again.value == "new"

    async def test_its_event_says_when(
        self, fields: FieldStore, events: SqlEventLog, clock: FakeClock
    ) -> None:
        await set_field(fields)
        await forget_field(fields, erasure=GRACE)

        latest, _ = await events.recent(ACCOUNT, PROFILE, limit=1)
        assert latest[0].detail == GRACE.describe(clock.now())


class TestTheSweepQuery:
    async def test_it_is_bounded_and_takes_the_oldest_due_first(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        """The bug, named: one sweep could hold the database thread for an unbounded backlog."""
        for index in range(5):
            await set_field(fields, key=f"key{index}")
            await forget_field(fields, f"key{index}", erasure=GRACE)
            clock.advance(timedelta(minutes=1))
        clock.advance(timedelta(days=8))

        assert await fields.purge_due(now=clock.now(), limit=2) == 2
        assert await fields.get(ACCOUNT, PROFILE, "key0", include_forgotten=True) is None
        assert await fields.get(ACCOUNT, PROFILE, "key1", include_forgotten=True) is None
        assert await fields.get(ACCOUNT, PROFILE, "key2", include_forgotten=True) is not None
        assert await fields.purge_due(now=clock.now(), limit=2) == 2
        assert await fields.purge_due(now=clock.now(), limit=2) == 1
        assert await fields.purge_due(now=clock.now(), limit=2) == 0

    async def test_each_account_waits_out_its_own_window(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields, account_id=ACCOUNT)
        await set_field(fields, account_id=OTHER_ACCOUNT)
        await forget_field(fields, erasure=GRACE, account_id=ACCOUNT)
        await forget_field(
            fields,
            erasure=ErasurePolicy(ErasureMode.GRACE, grace_days=30),
            account_id=OTHER_ACCOUNT,
        )

        clock.advance(timedelta(days=8))

        assert await fields.purge_due(now=clock.now(), limit=100) == 1
        assert await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True) is None
        assert await fields.get(OTHER_ACCOUNT, PROFILE, "voice", include_forgotten=True) is not None

    async def test_a_live_row_is_never_touched(self, fields: FieldStore, clock: FakeClock) -> None:
        await set_field(fields)
        clock.advance(timedelta(days=3650))

        assert await fields.purge_due(now=clock.now(), limit=100) == 0
        assert await fields.get(ACCOUNT, PROFILE, "voice") is not None


class TestNeverRetroactive:
    async def test_choosing_immediate_later_leaves_what_is_already_waiting(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields, key="old")
        await forget_field(fields, "old", erasure=GRACE)
        await set_field(fields, key="new")
        await forget_field(fields, "new", erasure=IMMEDIATE)

        assert await fields.get(ACCOUNT, PROFILE, "old", include_forgotten=True) is not None
        assert await fields.purge_due(now=clock.now(), limit=100) == 0

    async def test_leaving_tombstone_later_schedules_nothing_already_tombstoned(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields, key="kept")
        await forget_field(fields, "kept", erasure=TOMBSTONE)
        await set_field(fields, key="later")
        await forget_field(fields, "later", erasure=GRACE)
        clock.advance(timedelta(days=3650))

        assert await fields.purge_due(now=clock.now(), limit=100) == 1
        assert await fields.get(ACCOUNT, PROFILE, "kept", include_forgotten=True) is not None


class TestImmediate:
    async def test_the_field_is_gone_before_the_call_returns(
        self, fields: FieldStore, database: Database
    ) -> None:
        await set_field(fields)

        await forget_field(fields, erasure=IMMEDIATE)

        assert await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True) is None
        assert await rows(database, "fields") == 0
        assert await rows(database, "fields_fts") == 0

    async def test_the_note_is_gone_before_the_call_returns(
        self, notes: NoteStore, database: Database
    ) -> None:
        note = await write_note(notes)

        await forget_note(notes, note.note_id, erasure=IMMEDIATE)

        assert await notes.get(ACCOUNT, PROFILE, note.note_id, include_forgotten=True) is None
        assert await rows(database, "notes_fts") == 0

    async def test_the_record_that_it_happened_outlives_it(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)
        await forget_field(fields, erasure=IMMEDIATE)

        latest, _ = await events.recent(ACCOUNT, PROFILE, limit=1)
        assert latest[0].action is EventAction.FIELD_FORGOTTEN
        assert latest[0].detail == "erased"

    async def test_something_already_forgotten_cannot_be_erased_again(
        self, fields: FieldStore, notes: NoteStore
    ) -> None:
        await set_field(fields)
        note = await write_note(notes)
        await forget_field(fields, erasure=GRACE)
        await forget_note(notes, note.note_id, erasure=GRACE)

        with pytest.raises(FieldNotFoundError):
            await forget_field(fields, erasure=IMMEDIATE)
        with pytest.raises(NoteNotFoundError):
            await forget_note(notes, note.note_id, erasure=IMMEDIATE)
        assert await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True) is not None


class TestLoggedValues:
    async def test_off_a_revision_records_no_value(
        self, fields: FieldStore, notes: NoteStore, events: SqlEventLog
    ) -> None:
        """The bug, named: the event log kept a value for somebody who never asked it to."""
        await set_field(fields, value="dry")
        await set_field(fields, value="warm")
        note = await write_note(notes, body="first")
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="second",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        logged, _ = await events.recent(ACCOUNT, PROFILE)
        assert all(event.old_value is None for event in logged)

    async def test_on_a_field_revision_keeps_the_value_it_replaced(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields, value=["dry", "brief"])
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="how it speaks",
            value="warm",
            source=Source.OWNER,
            asserted_by=ASSERTED_BY,
            log_values=True,
        )

        latest, _ = await events.recent(ACCOUNT, PROFILE, limit=1)
        assert latest[0].action is EventAction.FIELD_REVISED
        assert latest[0].old_value is not None
        assert json.loads(latest[0].old_value) == ["dry", "brief"]

    async def test_on_a_pin_alone_replaced_no_value_and_keeps_none(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="how it speaks",
            value="dry and concise",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            pinned=True,
            log_values=True,
        )

        latest, _ = await events.recent(ACCOUNT, PROFILE, limit=1)
        assert latest[0].action is EventAction.FIELD_REVISED
        assert latest[0].old_value is None

    async def test_on_a_note_revision_keeps_the_body_it_replaced(
        self, notes: NoteStore, events: SqlEventLog
    ) -> None:
        note = await write_note(notes, body="first draft")
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="second draft",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            log_values=True,
        )
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            pinned=True,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            log_values=True,
        )

        logged, _ = await events.recent(ACCOUNT, PROFILE, limit=2)
        assert logged[0].old_value is None, "a pin replaced no body"
        assert logged[1].old_value is not None
        assert json.loads(logged[1].old_value) == "first draft"

    async def test_destroying_a_field_strips_them_and_keeps_the_events(
        self, fields: FieldStore, events: SqlEventLog, clock: FakeClock
    ) -> None:
        """The bug, named: a destroyed field's old value survived in the event that logged it."""
        await set_field(fields, value="first")
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="how it speaks",
            value="second",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            log_values=True,
        )
        before, _ = await events.recent(ACCOUNT, PROFILE)
        await forget_field(fields, erasure=GRACE)
        clock.advance(timedelta(days=8))

        await fields.purge_due(now=clock.now(), limit=100)

        after, _ = await events.recent(ACCOUNT, PROFILE)
        assert len(after) == len(before) + 1
        assert all(event.old_value is None for event in after)

    async def test_destroying_a_note_strips_only_that_notes(
        self, notes: NoteStore, events: SqlEventLog
    ) -> None:
        kept = await write_note(notes, body="kept first")
        doomed = await write_note(notes, body="doomed first")
        for note in (kept, doomed):
            await notes.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=note.note_id,
                body="revised",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
                log_values=True,
            )

        await forget_note(notes, doomed.note_id, erasure=IMMEDIATE)

        logged, _ = await events.recent(ACCOUNT, PROFILE)
        holding = {event.subject for event in logged if event.old_value is not None}
        assert holding == {kept.note_id}

    async def test_a_field_strip_never_reaches_a_note_with_the_same_subject(
        self, fields: FieldStore, events: SqlEventLog, database: Database
    ) -> None:
        # A field key and a note id share the subject column. Seeded by hand, because a
        # real note id cannot collide with a normalized key -- the guard is what makes
        # that a fact rather than a coincidence.
        await database.transact(
            lambda connection: events.append(
                connection,
                action=EventAction.NOTE_REVISED,
                account_id=ACCOUNT,
                profile=PROFILE,
                subject="voice",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
                old_value='"a note body"',
            )
        )
        await set_field(fields)

        await forget_field(fields, erasure=IMMEDIATE)

        logged, _ = await events.recent(ACCOUNT, PROFILE)
        assert [event.old_value for event in logged if event.old_value] == ['"a note body"']


SENTINEL = "ZORBLAX the persona once said something it regrets 7741"
WORD = b"zorblax"
"""The sentinel as the full-text index holds it: one word, lowercased by the tokenizer."""


def on_disk(database: Database, needle: bytes) -> bool:
    """Whether ``needle`` is anywhere in the database file or its write-ahead log."""
    wal = database.path.with_name(database.path.name + "-wal")
    return needle in database.path.read_bytes() or (wal.exists() and needle in wal.read_bytes())


async def stored_beside_neighbours(fields: FieldStore, notes: NoteStore, database: Database) -> str:
    """A note and a field holding the sentinel, among live neighbours, checkpointed to disk.

    The neighbours matter: a page with nothing else on it is freed whole, which hides the
    case where a deleted cell's bytes stay in a page that is still in use.
    """
    for index in range(30):
        await write_note(notes, body=f"an ordinary neighbour note number {index}")
    note = await write_note(notes, body=SENTINEL)
    await set_field(fields, value=SENTINEL)
    await database.checkpoint_truncate()
    assert on_disk(database, SENTINEL.encode()), "never stored"
    assert on_disk(database, WORD), "never indexed"
    return note.note_id


class TestTheBytes:
    """Destroyed means gone from the file, its write-ahead log, and the search index.

    About the file rather than the code, so no unit test of a query can stand in for these.
    """

    async def test_an_erased_value_is_in_neither_the_file_nor_its_write_ahead_log(
        self, fields: FieldStore, notes: NoteStore, database: Database
    ) -> None:
        """The bug, named: an erased row's text was still on disk, readable with grep.

        A DELETE only unlinks the cell unless secure_delete is on, and the page as it was
        stays in the -wal until a truncating checkpoint. Both halves are needed.
        """
        note_id = await stored_beside_neighbours(fields, notes, database)

        await forget_note(notes, note_id, erasure=IMMEDIATE)
        await forget_field(fields, erasure=IMMEDIATE)

        assert not on_disk(database, SENTINEL.encode())

    async def test_an_erased_rows_words_leave_the_search_index(
        self, fields: FieldStore, notes: NoteStore, database: Database
    ) -> None:
        """The bug, named: an erased row's words survived in the full-text index's segments.

        An FTS5 delete writes a marker and leaves the document's terms where they were
        until a merge reaches them, so a destroyed note's every word stayed greppable.
        """
        note_id = await stored_beside_neighbours(fields, notes, database)

        await forget_note(notes, note_id, erasure=IMMEDIATE)
        await forget_field(fields, erasure=IMMEDIATE)

        assert not on_disk(database, WORD)

    async def test_a_swept_row_leaves_nothing_behind(
        self, fields: FieldStore, notes: NoteStore, database: Database, clock: FakeClock
    ) -> None:
        note_id = await stored_beside_neighbours(fields, notes, database)
        await forget_note(notes, note_id, erasure=GRACE)
        await forget_field(fields, erasure=GRACE)
        clock.advance(timedelta(days=7))

        await Sweeper(database=database, fields=fields, notes=notes, clock=clock).sweep_once()

        assert not on_disk(database, SENTINEL.encode())
        assert not on_disk(database, WORD)

    async def test_a_field_destroyed_by_setting_its_key_again_leaves_nothing_behind(
        self, fields: FieldStore, notes: NoteStore, database: Database, clock: FakeClock
    ) -> None:
        """The bug, named: a past-due field destroyed by a write stayed in the write-ahead log.

        The write destroyed it, so the sweep that would have truncated the log found nothing
        to destroy and never did.
        """
        note_id = await stored_beside_neighbours(fields, notes, database)
        await forget_note(notes, note_id, erasure=IMMEDIATE)
        await forget_field(fields, erasure=GRACE)
        clock.advance(timedelta(days=7))
        assert on_disk(database, SENTINEL.encode()), "the field is the copy left"

        await set_field(fields, value="something else entirely")

        assert not on_disk(database, SENTINEL.encode())
        assert not on_disk(database, WORD)

    async def test_a_deleted_personas_rows_and_words_leave_nothing_behind(
        self, fields: FieldStore, notes: NoteStore, database: Database, events: SqlEventLog
    ) -> None:
        """The bug, named: a deleted persona's words survived in both search indexes."""
        await stored_beside_neighbours(fields, notes, database)

        assert await PersonaStore(database=database, events=events).delete(ACCOUNT, PROFILE)

        assert not on_disk(database, SENTINEL.encode())
        assert not on_disk(database, WORD)
