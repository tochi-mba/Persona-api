"""The note store: accrual, partial revision, and caps that hold under concurrency."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.errors import (
    CredentialRefusedError,
    InvalidNoteError,
    LimitExceededError,
    NoteNotFoundError,
)
from persona_api.domain.notes import NoteKind
from persona_api.domain.provenance import Source
from persona_api.events.log import EventAction
from persona_api.memory.filters import NoteFilters
from persona_api.memory.notes import NoteStore
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, write_note

if TYPE_CHECKING:
    from persona_api.events.sql_log import SqlEventLog
    from persona_api.storage.database import Database
    from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.usefixtures("personas")


class TestWriting:
    async def test_a_note_is_stored_as_written(self, notes: NoteStore) -> None:
        note = await write_note(notes, body="  they went quiet  ")

        assert note.body == "they went quiet"
        assert note.kind is NoteKind.EPISODE
        assert note.revision == 1

    async def test_two_notes_saying_the_same_thing_are_two_notes(self, notes: NoteStore) -> None:
        # Which is the difference between a note and a field. A field is identified by
        # its key and replaced; a note is an event in time and accrues.
        first = await write_note(notes, body="they went quiet")
        second = await write_note(notes, body="they went quiet")

        assert first.note_id != second.note_id
        assert await notes.count(ACCOUNT, PROFILE) == 2

    async def test_an_empty_body_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(InvalidNoteError):
            await write_note(notes, body="   ")

    async def test_a_body_over_the_configured_length_is_refused(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=50,
            max_notes=5000,
            max_pinned=20,
        )

        with pytest.raises(InvalidNoteError, match="50"):
            await write_note(store, body="b" * 51)

    async def test_a_credential_shaped_body_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await write_note(notes, body="my key is " + "AKIA" + "IOSFODNN7EXAMPLE")

    async def test_nothing_is_written_when_a_body_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(CredentialRefusedError):
            await write_note(notes, body="AKIA" + "IOSFODNN7EXAMPLE")

        assert await notes.count(ACCOUNT, PROFILE) == 0


class TestRevising:
    async def test_it_changes_only_what_is_named(self, notes: NoteStore) -> None:
        # A caller that had to resend the body to change the pin would be one race away
        # from overwriting an edit it never saw.
        note = await write_note(notes, body="the original", kind=NoteKind.EPISODE)

        revised = await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            pinned=True,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert revised.body == "the original"
        assert revised.kind is NoteKind.EPISODE
        assert revised.pinned is True
        assert revised.revision == 2

    async def test_it_can_change_the_kind_alone(self, notes: NoteStore) -> None:
        note = await write_note(notes, kind=NoteKind.EPISODE)

        revised = await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            kind=NoteKind.LESSON,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert revised.kind is NoteKind.LESSON

    async def test_revising_to_the_same_values_does_not_bump_the_revision(
        self, notes: NoteStore
    ) -> None:
        note = await write_note(notes, body="unchanged")

        revised = await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="unchanged",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert revised.revision == 1

    async def test_the_created_at_survives_a_revision(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        note = await write_note(notes)
        clock.advance(3600)

        revised = await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="edited",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert revised.created_at == note.created_at
        assert revised.updated_at > note.updated_at

    async def test_a_credential_pasted_in_by_an_edit_is_refused(self, notes: NoteStore) -> None:
        note = await write_note(notes)

        with pytest.raises(CredentialRefusedError):
            await notes.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=note.note_id,
                body="AKIA" + "IOSFODNN7EXAMPLE",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_revising_a_note_that_is_not_there_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(NoteNotFoundError):
            await notes.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id="note_nothing",
                body="x",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_revising_a_forgotten_note_is_refused(self, notes: NoteStore) -> None:
        note = await write_note(notes)
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        with pytest.raises(NoteNotFoundError):
            await notes.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=note.note_id,
                body="x",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )


class TestForgetting:
    async def test_a_forgotten_note_is_absent_by_default(self, notes: NoteStore) -> None:
        note = await write_note(notes)
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert await notes.get(ACCOUNT, PROFILE, note.note_id) is None

    async def test_it_comes_back_when_asked_for(self, notes: NoteStore) -> None:
        note = await write_note(notes)
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        found = await notes.get(ACCOUNT, PROFILE, note.note_id, include_forgotten=True)

        assert found is not None
        assert found.forgotten_at is not None

    async def test_forgetting_twice_is_refused_the_second_time(self, notes: NoteStore) -> None:
        note = await write_note(notes)
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        with pytest.raises(NoteNotFoundError):
            await notes.forget(
                ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )

    async def test_forgetting_one_that_never_existed_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(NoteNotFoundError):
            await notes.forget(
                ACCOUNT, PROFILE, "note_nothing", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )


class TestIsolation:
    async def test_one_account_cannot_read_anothers_note(self, notes: NoteStore) -> None:
        note = await write_note(notes, account_id=OTHER_ACCOUNT)

        assert await notes.get(ACCOUNT, PROFILE, note.note_id) is None

    async def test_one_account_cannot_revise_anothers_note(self, notes: NoteStore) -> None:
        note = await write_note(notes, account_id=OTHER_ACCOUNT)

        with pytest.raises(NoteNotFoundError):
            await notes.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=note.note_id,
                body="tampered",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_one_account_cannot_forget_anothers_note(self, notes: NoteStore) -> None:
        # The same error a note that never existed would give. A distinguishable answer
        # would confirm that another account has a note with this id.
        note = await write_note(notes, account_id=OTHER_ACCOUNT)

        with pytest.raises(NoteNotFoundError):
            await notes.forget(
                ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )

    async def test_one_account_cannot_list_anothers_notes(self, notes: NoteStore) -> None:
        await write_note(notes, account_id=OTHER_ACCOUNT)

        page = await notes.list_for_persona(ACCOUNT, PROFILE, filters=NoteFilters(), limit=20)

        assert page.items == []

    async def test_one_profile_cannot_read_anothers_note(self, notes: NoteStore) -> None:
        note = await write_note(notes, profile="home")

        assert await notes.get(ACCOUNT, PROFILE, note.note_id) is None


class TestCaps:
    async def test_the_note_cap_holds(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=3,
            max_pinned=20,
        )
        for index in range(3):
            await write_note(store, body=f"note {index}")

        with pytest.raises(LimitExceededError, match="3 notes"):
            await write_note(store, body="one too many")

    async def test_the_note_cap_holds_under_concurrent_writes(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5,
            max_pinned=20,
        )

        await asyncio.gather(
            *(write_note(store, body=f"note {index}") for index in range(30)),
            return_exceptions=True,
        )

        assert await store.count(ACCOUNT, PROFILE) == 5

    async def test_forgetting_makes_room_again(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # The cap counts live rows, so a persona full of memories can still learn
        # something new once it has let something go.
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=1,
            max_pinned=20,
        )
        note = await write_note(store, body="the only one")
        await store.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        replacement = await write_note(store, body="a replacement")

        assert replacement.body == "a replacement"

    async def test_the_pinned_cap_holds(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=2,
        )
        for index in range(2):
            await write_note(store, body=f"note {index}", pinned=True)

        with pytest.raises(LimitExceededError, match="pinned"):
            await write_note(store, body="one too many", pinned=True)

    async def test_pinning_by_revision_is_capped_too(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # The cap has to hold on both routes to a pinned row, or the one that is not
        # checked is the one everybody uses.
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=1,
        )
        await write_note(store, body="already pinned", pinned=True)
        later = await write_note(store, body="not yet pinned")

        with pytest.raises(LimitExceededError, match="pinned"):
            await store.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=later.note_id,
                pinned=True,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_the_pinned_cap_holds_under_concurrent_writes(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=3,
        )

        await asyncio.gather(
            *(write_note(store, body=f"note {index}", pinned=True) for index in range(20)),
            return_exceptions=True,
        )

        assert len(await store.pinned(ACCOUNT, PROFILE)) == 3

    async def test_unpinning_a_note_does_not_need_room(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=1,
        )
        note = await write_note(store, body="pinned", pinned=True)

        revised = await store.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            pinned=False,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert revised.pinned is False

    async def test_a_per_call_pin_ceiling_wins_over_the_constructor(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=20,
        )
        await write_note(store, body="one", pinned=True, max_pinned=1)

        with pytest.raises(LimitExceededError, match="at most 1 pinned"):
            await write_note(store, body="two", pinned=True, max_pinned=1)

    async def test_revising_honours_the_per_call_pin_ceiling(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=20,
        )
        await write_note(store, body="already pinned", pinned=True, max_pinned=1)
        later = await write_note(store, body="not yet")

        with pytest.raises(LimitExceededError, match="at most 1 pinned"):
            await store.revise(
                account_id=ACCOUNT,
                profile=PROFILE,
                note_id=later.note_id,
                pinned=True,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
                max_pinned=1,
            )

    async def test_two_accounts_can_have_different_pin_ceilings_on_the_same_store(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = NoteStore(
            database=database,
            clock=clock,
            events=events,
            max_body_chars=4000,
            max_notes=5000,
            max_pinned=20,
        )

        await asyncio.gather(
            write_note(store, account_id=ACCOUNT, body="a1", pinned=True, max_pinned=1),
            write_note(store, account_id=ACCOUNT, body="a2", pinned=True, max_pinned=1),
            write_note(store, account_id=OTHER_ACCOUNT, body="b1", pinned=True, max_pinned=3),
            write_note(store, account_id=OTHER_ACCOUNT, body="b2", pinned=True, max_pinned=3),
            write_note(store, account_id=OTHER_ACCOUNT, body="b3", pinned=True, max_pinned=3),
            write_note(store, account_id=OTHER_ACCOUNT, body="b4", pinned=True, max_pinned=3),
            return_exceptions=True,
        )

        assert len(await store.pinned(ACCOUNT, PROFILE)) == 1
        assert len(await store.pinned(OTHER_ACCOUNT, PROFILE)) == 3


class TestEvents:
    async def test_writing_records_it(self, notes: NoteStore, events: SqlEventLog) -> None:
        note = await write_note(notes)

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert recorded[0].action is EventAction.NOTE_WRITTEN
        assert recorded[0].subject == note.note_id

    async def test_revising_records_it(self, notes: NoteStore, events: SqlEventLog) -> None:
        note = await write_note(notes)
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="edited",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert recorded[0].action is EventAction.NOTE_REVISED

    async def test_forgetting_records_it(self, notes: NoteStore, events: SqlEventLog) -> None:
        note = await write_note(notes)
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert recorded[0].action is EventAction.NOTE_FORGOTTEN

    async def test_no_event_ever_carries_the_body(
        self, notes: NoteStore, events: SqlEventLog
    ) -> None:
        # An event log is read more often and kept longer than the rows it describes,
        # and it is the one table here with no tombstone.
        await write_note(notes, body="a distinctive private thing")

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert "distinctive" not in recorded[0].detail
        assert "distinctive" not in recorded[0].subject

    async def test_a_refused_write_records_no_event(
        self, notes: NoteStore, events: SqlEventLog
    ) -> None:
        with pytest.raises(CredentialRefusedError):
            await write_note(notes, body="AKIA" + "IOSFODNN7EXAMPLE")

        assert await events.count() == 0
