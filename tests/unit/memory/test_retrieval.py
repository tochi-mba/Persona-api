"""Filters, pagination and search -- the half the service exists for."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.errors import InvalidCursorError, InvalidSearchError
from persona_api.domain.notes import NoteKind
from persona_api.domain.provenance import Source
from persona_api.memory.filters import FieldFilters, NoteFilters
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, set_field, write_note

if TYPE_CHECKING:
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.usefixtures("personas")


class TestFieldFilters:
    async def test_no_filter_returns_everything_live(self, fields: FieldStore) -> None:
        for index in range(3):
            await set_field(fields, key=f"key{index}")

        page = await fields.list_for_persona(ACCOUNT, PROFILE, filters=FieldFilters(), limit=20)

        assert len(page.items) == 3

    async def test_by_source(self, fields: FieldStore) -> None:
        await set_field(fields, key="said", source=Source.OWNER)
        await set_field(fields, key="inferred", source=Source.ASSISTANT)

        page = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(source=Source.OWNER), limit=20
        )

        assert [field.key for field in page.items] == ["said"]

    async def test_by_pinned(self, fields: FieldStore) -> None:
        await set_field(fields, key="pinned_one", pinned=True)
        await set_field(fields, key="ordinary")

        pinned = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(pinned=True), limit=20
        )
        unpinned = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(pinned=False), limit=20
        )

        assert [field.key for field in pinned.items] == ["pinned_one"]
        assert [field.key for field in unpinned.items] == ["ordinary"]

    async def test_by_key_prefix(self, fields: FieldStore) -> None:
        await set_field(fields, key="forms_of_address")
        await set_field(fields, key="forms_of_greeting")
        await set_field(fields, key="voice")

        page = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(key_prefix="forms_"), limit=20
        )

        assert sorted(field.key for field in page.items) == [
            "forms_of_address",
            "forms_of_greeting",
        ]

    async def test_by_a_batch_of_named_keys(self, fields: FieldStore) -> None:
        # ?keys=voice,tone exists so an assistant makes one call rather than thirty.
        for key in ("voice", "tone", "pace", "humour"):
            await set_field(fields, key=key)

        page = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(keys=("Voice", "tone")), limit=20
        )

        assert sorted(field.key for field in page.items) == ["tone", "voice"]

    async def test_by_a_time_range_on_updated_at(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        # Fields range-filter on updated_at: a field is current state, and when it last
        # changed is what you ask about.
        await set_field(fields, key="old")
        clock.advance(timedelta(days=2))
        boundary = clock.now()
        await set_field(fields, key="new")

        page = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(since=boundary), limit=20
        )

        assert [field.key for field in page.items] == ["new"]

    async def test_until_is_exclusive_so_two_ranges_never_overlap(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields, key="old")
        clock.advance(timedelta(days=2))
        boundary = clock.now()
        await set_field(fields, key="new")

        before = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(until=boundary), limit=20
        )
        after = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(since=boundary), limit=20
        )

        assert [field.key for field in before.items] == ["old"]
        assert [field.key for field in after.items] == ["new"]

    async def test_forgotten_rows_are_absent_by_default_and_present_when_asked(
        self, fields: FieldStore
    ) -> None:
        await set_field(fields, key="kept")
        await set_field(fields, key="dropped")
        await fields.forget(
            ACCOUNT, PROFILE, "dropped", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        default = await fields.list_for_persona(ACCOUNT, PROFILE, filters=FieldFilters(), limit=20)
        everything = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(include_forgotten=True), limit=20
        )

        assert [field.key for field in default.items] == ["kept"]
        assert sorted(field.key for field in everything.items) == ["dropped", "kept"]

    async def test_filters_combine(self, fields: FieldStore) -> None:
        await set_field(fields, key="forms_said", source=Source.OWNER, pinned=True)
        await set_field(fields, key="forms_guessed", source=Source.ASSISTANT, pinned=True)
        await set_field(fields, key="voice_said", source=Source.OWNER, pinned=True)
        await set_field(fields, key="forms_unpinned", source=Source.OWNER)

        page = await fields.list_for_persona(
            ACCOUNT,
            PROFILE,
            filters=FieldFilters(source=Source.OWNER, pinned=True, key_prefix="forms_"),
            limit=20,
        )

        assert [field.key for field in page.items] == ["forms_said"]


class TestNoteFilters:
    async def test_by_kind(self, notes: NoteStore) -> None:
        await write_note(notes, body="it happened", kind=NoteKind.EPISODE)
        await write_note(notes, body="do it differently", kind=NoteKind.LESSON)

        page = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(kind=NoteKind.LESSON), limit=20
        )

        assert [note.body for note in page.items] == ["do it differently"]

    async def test_by_a_time_range_on_created_at(self, notes: NoteStore, clock: FakeClock) -> None:
        # Notes range-filter on created_at: a note is something that happened, and when
        # it happened is what you ask about. That is the one place the two shapes differ.
        await write_note(notes, body="last week")
        clock.advance(timedelta(days=7))
        boundary = clock.now()
        await write_note(notes, body="today")

        page = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(since=boundary), limit=20
        )

        assert [note.body for note in page.items] == ["today"]

    async def test_by_source(self, notes: NoteStore) -> None:
        await write_note(notes, body="they told me", source=Source.OWNER)
        await write_note(notes, body="I worked it out", source=Source.ASSISTANT)

        page = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(source=Source.OWNER), limit=20
        )

        assert [note.body for note in page.items] == ["they told me"]

    async def test_by_pinned(self, notes: NoteStore) -> None:
        await write_note(notes, body="in the prompt", pinned=True)
        await write_note(notes, body="in the archive")

        pinned = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(pinned=True), limit=20
        )
        unpinned = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(pinned=False), limit=20
        )

        assert [note.body for note in pinned.items] == ["in the prompt"]
        assert [note.body for note in unpinned.items] == ["in the archive"]

    async def test_until_is_exclusive_for_notes_too(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        await write_note(notes, body="earlier")
        clock.advance(timedelta(days=1))
        boundary = clock.now()
        await write_note(notes, body="later")

        before = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(until=boundary), limit=20
        )
        after = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(since=boundary), limit=20
        )

        assert [note.body for note in before.items] == ["earlier"]
        assert [note.body for note in after.items] == ["later"]

    async def test_forgotten_notes_are_absent_by_default_and_present_when_asked(
        self, notes: NoteStore
    ) -> None:
        kept = await write_note(notes, body="kept")
        dropped = await write_note(notes, body="dropped")
        await notes.forget(
            ACCOUNT, PROFILE, dropped.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        default = await notes.list_for_persona(ACCOUNT, PROFILE, filters=NoteFilters(), limit=20)
        everything = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(include_forgotten=True), limit=20
        )

        assert [note.note_id for note in default.items] == [kept.note_id]
        assert len(everything.items) == 2

    async def test_note_filters_combine(self, notes: NoteStore) -> None:
        await write_note(notes, body="a", kind=NoteKind.LESSON, source=Source.OWNER, pinned=True)
        await write_note(
            notes, body="b", kind=NoteKind.LESSON, source=Source.ASSISTANT, pinned=True
        )
        await write_note(notes, body="c", kind=NoteKind.EPISODE, source=Source.OWNER, pinned=True)
        await write_note(notes, body="d", kind=NoteKind.LESSON, source=Source.OWNER)

        page = await notes.list_for_persona(
            ACCOUNT,
            PROFILE,
            filters=NoteFilters(kind=NoteKind.LESSON, source=Source.OWNER, pinned=True),
            limit=20,
        )

        assert [note.body for note in page.items] == ["a"]

    async def test_editing_a_note_does_not_move_it_in_time(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        # The consequence of ordering on created_at, and the reason for it: correcting
        # a typo in an old note must not make it today's news.
        old = await write_note(notes, body="last week")
        clock.advance(timedelta(days=7))
        await write_note(notes, body="today")
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=old.note_id,
            body="last week, corrected",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        page = await notes.list_for_persona(ACCOUNT, PROFILE, filters=NoteFilters(), limit=20)

        assert [note.body for note in page.items] == ["today", "last week, corrected"]


class TestPagination:
    async def test_it_walks_every_row_exactly_once(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        for index in range(25):
            clock.advance(1)
            await write_note(notes, body=f"note {index:02d}")

        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = await notes.list_for_persona(
                ACCOUNT, PROFILE, filters=NoteFilters(), limit=10, cursor=cursor
            )
            seen.extend(note.body for note in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == [f"note {index:02d}" for index in reversed(range(25))]

    async def test_a_write_during_a_walk_shows_no_row_twice_and_skips_none(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        # The whole reason for keyset rather than offset. An offset shifts when a row
        # is inserted above it, so the walk shows one row twice and skips another --
        # silently, and only under concurrency.
        for index in range(20):
            clock.advance(1)
            await write_note(notes, body=f"note {index:02d}")

        first = await notes.list_for_persona(ACCOUNT, PROFILE, filters=NoteFilters(), limit=10)
        clock.advance(1)
        await write_note(notes, body="arrived mid-walk")
        second = await notes.list_for_persona(
            ACCOUNT, PROFILE, filters=NoteFilters(), limit=10, cursor=first.next_cursor
        )

        bodies = [note.body for note in first.items + second.items]
        assert len(bodies) == len(set(bodies))
        assert sorted(bodies) == sorted(f"note {index:02d}" for index in range(20))

    async def test_a_full_final_page_hands_back_no_cursor(
        self, notes: NoteStore, clock: FakeClock
    ) -> None:
        # The off-by-one a "cursor whenever the page is full" rule gets wrong: the
        # caller then makes one more request for an empty page, every single time.
        for index in range(10):
            clock.advance(1)
            await write_note(notes, body=f"note {index}")

        page = await notes.list_for_persona(ACCOUNT, PROFILE, filters=NoteFilters(), limit=10)

        assert len(page.items) == 10
        assert page.next_cursor is None

    async def test_it_pages_fields_too(self, fields: FieldStore, clock: FakeClock) -> None:
        for index in range(12):
            clock.advance(1)
            await set_field(fields, key=f"key{index:02d}")

        first = await fields.list_for_persona(ACCOUNT, PROFILE, filters=FieldFilters(), limit=5)
        second = await fields.list_for_persona(
            ACCOUNT, PROFILE, filters=FieldFilters(), limit=5, cursor=first.next_cursor
        )

        assert len(first.items) == 5
        assert len(second.items) == 5
        assert {f.key for f in first.items}.isdisjoint({f.key for f in second.items})

    async def test_a_forged_cursor_is_refused(self, notes: NoteStore) -> None:
        with pytest.raises(InvalidCursorError):
            await notes.list_for_persona(
                ACCOUNT, PROFILE, filters=NoteFilters(), limit=10, cursor="not-a-cursor!!"
            )

    async def test_a_forged_cursor_is_refused_for_fields_too(self, fields: FieldStore) -> None:
        with pytest.raises(InvalidCursorError):
            await fields.list_for_persona(
                ACCOUNT, PROFILE, filters=FieldFilters(), limit=10, cursor="not-a-cursor!!"
            )


class TestSearch:
    async def test_it_finds_a_word_in_a_note(self, notes: NoteStore) -> None:
        await write_note(notes, body="they went quiet when I suggested a rewrite")
        await write_note(notes, body="they liked the diagram")

        found = await notes.search(ACCOUNT, query="rewrite", limit=10)

        assert [note.body for note in found] == ["they went quiet when I suggested a rewrite"]

    async def test_stemming_finds_a_different_ending(self, notes: NoteStore) -> None:
        # porter unicode61, so a search for "preferring" finds "prefer". That covers
        # most of the near-misses anybody actually hits, which is the argument in
        # ADR-0006 for not reaching for embeddings.
        await write_note(notes, body="they prefer concise answers")

        assert await notes.search(ACCOUNT, query="preferring", limit=10)

    async def test_it_finds_a_word_inside_a_list_value(self, fields: FieldStore) -> None:
        # The index holds a rendered projection rather than the JSON, so a word inside
        # a list is findable without the tokenizer ever meeting a bracket.
        await set_field(fields, key="favourite_topics", value=["jazz", "ambient"])

        found = await fields.search(ACCOUNT, query="ambient", limit=10)

        assert [field.key for field in found] == ["favourite_topics"]

    async def test_it_finds_a_field_by_its_key_and_by_its_description(
        self, fields: FieldStore
    ) -> None:
        await set_field(
            fields, key="forms_of_address", description="what to call them", value="Alex"
        )

        assert await fields.search(ACCOUNT, query="address", limit=10)
        assert await fields.search(ACCOUNT, query="call", limit=10)

    async def test_json_punctuation_is_not_searchable(self, fields: FieldStore) -> None:
        await set_field(fields, key="home", value={"city": "Lisbon"})

        assert await fields.search(ACCOUNT, query="Lisbon", limit=10)
        with pytest.raises(InvalidSearchError):
            await fields.search(ACCOUNT, query="{}", limit=10)

    async def test_a_search_can_be_scoped_to_one_persona(self, notes: NoteStore) -> None:
        await write_note(notes, profile=PROFILE, body="a rewrite at work")
        await write_note(notes, profile="home", body="a rewrite at home")

        scoped = await notes.search(ACCOUNT, query="rewrite", profile=PROFILE, limit=10)
        everywhere = await notes.search(ACCOUNT, query="rewrite", limit=10)

        assert [note.profile for note in scoped] == [PROFILE]
        assert sorted(note.profile for note in everywhere) == ["home", PROFILE]


class TestSearchIsolation:
    """The one place isolation is not structural.

    Every store method takes an account id and it is part of the key, so a cross-account
    read cannot be expressed. Full-text search is the exception: there is one
    ``fields_fts`` and one ``notes_fts``, holding every account's text, and the account
    filter lives in the ``JOIN`` rather than in the ``MATCH``.

    That works. It is also exactly the kind of thing that breaks silently the first time
    somebody rewrites the query, which is why this class exists on its own.
    """

    async def test_one_accounts_search_never_returns_anothers_note(self, notes: NoteStore) -> None:
        await write_note(notes, account_id=OTHER_ACCOUNT, body="a secret about somebody else")

        assert await notes.search(ACCOUNT, query="secret", limit=10) == []

    async def test_one_accounts_search_never_returns_anothers_field(
        self, fields: FieldStore
    ) -> None:
        await set_field(fields, account_id=OTHER_ACCOUNT, key="voice", value="theirs alone")

        assert await fields.search(ACCOUNT, query="theirs", limit=10) == []

    async def test_a_shared_word_returns_only_your_own_rows(self, notes: NoteStore) -> None:
        # The realistic case: two accounts write about the same thing, and the index
        # holds both. Only the JOIN keeps them apart.
        await write_note(notes, account_id=ACCOUNT, body="the migration review went well")
        await write_note(notes, account_id=OTHER_ACCOUNT, body="the migration review went badly")

        found = await notes.search(ACCOUNT, query="migration", limit=10)

        assert [note.account_id for note in found] == [ACCOUNT]
        assert [note.body for note in found] == ["the migration review went well"]

    async def test_a_shared_word_returns_only_your_own_fields(self, fields: FieldStore) -> None:
        await set_field(fields, account_id=ACCOUNT, key="voice", value="mine is dry")
        await set_field(fields, account_id=OTHER_ACCOUNT, key="voice", value="theirs is dry")

        found = await fields.search(ACCOUNT, query="dry", limit=10)

        assert [field.account_id for field in found] == [ACCOUNT]
