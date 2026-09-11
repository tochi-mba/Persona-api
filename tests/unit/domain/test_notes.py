"""Note bodies and the closed set of kinds."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from persona_api.domain.errors import InvalidNoteError
from persona_api.domain.notes import (
    NOTE_ID_PREFIX,
    Note,
    NoteKind,
    new_note_id,
    normalize_body,
)
from persona_api.domain.provenance import Source


class TestNoteKinds:
    def test_the_set_is_closed(self) -> None:
        # Closed for the same reason a permission enum is closed: a free-string kind
        # accumulates "lesson", "Lesson" and "lessons", and the note filed under the
        # odd one out can never be filtered for again. Nobody notices until they go
        # looking for every lesson and one is missing.
        assert {kind.value for kind in NoteKind} == {"episode", "observation", "lesson"}

    def test_an_invented_kind_is_not_a_kind(self) -> None:
        with pytest.raises(ValueError, match="lessons"):
            NoteKind("lessons")


class TestBodies:
    def test_it_trims(self) -> None:
        assert normalize_body("  they went quiet  ", limit=100) == "they went quiet"

    @pytest.mark.parametrize("written", ["", "   ", "\n\t"])
    def test_an_empty_body_is_refused_rather_than_stored_as_absent(self, written: str) -> None:
        # Unlike card text, a blank body is an error rather than an absence: a note
        # with nothing in it is a row that will be read, ranked and rendered forever
        # without ever saying anything.
        with pytest.raises(InvalidNoteError):
            normalize_body(written, limit=100)

    def test_a_body_at_the_limit_is_kept(self) -> None:
        assert normalize_body("b" * 100, limit=100) == "b" * 100

    def test_a_body_over_the_limit_says_the_limit(self) -> None:
        with pytest.raises(InvalidNoteError, match="100"):
            normalize_body("b" * 101, limit=100)

    def test_the_limit_is_the_callers_rather_than_a_constant_here(self) -> None:
        # It is a prompt budget, which is a deployment's decision -- so it lives in
        # core.config and arrives as an argument. The domain reads no configuration.
        assert normalize_body("b" * 101, limit=200) == "b" * 101


class TestNoteIds:
    def test_an_id_is_opaque_and_prefixed(self) -> None:
        assert new_note_id().startswith(NOTE_ID_PREFIX)

    def test_two_notes_saying_the_same_thing_are_two_notes(self) -> None:
        # Which is exactly the difference between a note and a field: a field is
        # identified by its key and re-set, a note is identified by an id and accrues.
        assert new_note_id() != new_note_id()


class TestTheNoteType:
    def test_it_is_frozen(self) -> None:
        note = Note(
            note_id=new_note_id(),
            account_id="acct",
            profile="work",
            body="they went quiet when I suggested Rust",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by="persona",
            pinned=False,
            revision=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            forgotten_at=None,
        )

        with pytest.raises(AttributeError):
            note.body = "something else"  # type: ignore[misc]
