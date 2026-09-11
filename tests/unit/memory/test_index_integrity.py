"""That the full-text index and the tables agree, after every write path.

This class exists because of a design choice with a known cost. The index is maintained
by the stores, explicitly, in the same transaction as the row -- not by external-content
FTS5 tables and not by triggers, both for reasons in the ``memory.fields`` docstring.

What that buys is a projection rendered in Python, where it can be unit-tested. What it
costs is a whole bug class: a write path that forgets to call ``_index`` or ``_unindex``.
The failure is silent -- the table is right, the search is wrong -- and it is only ever
discovered by somebody searching for something that is definitely there.

So every write path gets an assertion here, including the one that is easiest to miss:
deleting the persona.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.domain.provenance import Source
from tests.conftest import ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, set_field, write_note

if TYPE_CHECKING:
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.storage.database import Database

pytestmark = pytest.mark.usefixtures("personas")


async def live_field_seqs(database: Database) -> set[int]:
    rows = await database.fetch_all("SELECT seq FROM fields WHERE forgotten_at IS NULL")
    return {row["seq"] for row in rows}


async def indexed_field_seqs(database: Database) -> set[int]:
    rows = await database.fetch_all("SELECT rowid FROM fields_fts")
    return {row["rowid"] for row in rows}


async def live_note_seqs(database: Database) -> set[int]:
    rows = await database.fetch_all("SELECT seq FROM notes WHERE forgotten_at IS NULL")
    return {row["seq"] for row in rows}


async def indexed_note_seqs(database: Database) -> set[int]:
    rows = await database.fetch_all("SELECT rowid FROM notes_fts")
    return {row["rowid"] for row in rows}


class TestFieldsIndexIntegrity:
    async def test_after_creating(self, fields: FieldStore, database: Database) -> None:
        await set_field(fields, key="voice")
        await set_field(fields, key="tone")

        assert await indexed_field_seqs(database) == await live_field_seqs(database)

    async def test_after_revising(self, fields: FieldStore, database: Database) -> None:
        # The one external-content tables get wrong: an update must remove the OLD text
        # as well as add the new, or the index holds both and a search for the old
        # value still finds the row.
        await set_field(fields, value="dry and concise")
        await set_field(fields, value="warmer now")

        assert await indexed_field_seqs(database) == await live_field_seqs(database)
        found = await fields.search(ACCOUNT, query="concise", limit=10)
        assert found == []

    async def test_after_forgetting(self, fields: FieldStore, database: Database) -> None:
        # A forgotten field that still came back in recall would make the tombstone
        # cosmetic -- the row would be hidden from listings and findable by search.
        await set_field(fields, key="voice")
        await set_field(fields, key="tone")
        await fields.forget(
            ACCOUNT, PROFILE, "tone", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert await indexed_field_seqs(database) == await live_field_seqs(database)
        assert await fields.search(ACCOUNT, query="warmth", limit=10) == []

    async def test_after_reviving(self, fields: FieldStore, database: Database) -> None:
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )
        await set_field(fields, value="remembered again")

        assert await indexed_field_seqs(database) == await live_field_seqs(database)
        assert await fields.search(ACCOUNT, query="remembered", limit=10)

    async def test_after_deleting_the_persona(self, fields: FieldStore, database: Database) -> None:
        # The one that gets missed. The rows go through ON DELETE CASCADE, which the
        # FTS table is not part of -- so without an explicit sweep the index keeps
        # every word of a persona somebody deleted.
        await set_field(fields, key="voice")
        await set_field(fields, key="tone")

        await database.execute(
            "DELETE FROM personas WHERE account_id = ? AND profile = ?", (ACCOUNT, PROFILE)
        )

        assert await live_field_seqs(database) == set()
        assert await indexed_field_seqs(database) == set()


class TestNotesIndexIntegrity:
    async def test_after_writing(self, notes: NoteStore, database: Database) -> None:
        await write_note(notes, body="first")
        await write_note(notes, body="second")

        assert await indexed_note_seqs(database) == await live_note_seqs(database)

    async def test_after_revising(self, notes: NoteStore, database: Database) -> None:
        note = await write_note(notes, body="they disliked the rewrite")
        await notes.revise(
            account_id=ACCOUNT,
            profile=PROFILE,
            note_id=note.note_id,
            body="they were fine with the rewrite",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert await indexed_note_seqs(database) == await live_note_seqs(database)
        assert await notes.search(ACCOUNT, query="disliked", limit=10) == []

    async def test_after_forgetting(self, notes: NoteStore, database: Database) -> None:
        note = await write_note(notes, body="a memory to drop")
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert await indexed_note_seqs(database) == await live_note_seqs(database)
        assert await notes.search(ACCOUNT, query="memory", limit=10) == []

    async def test_after_deleting_the_persona(self, notes: NoteStore, database: Database) -> None:
        await write_note(notes, body="first")
        await write_note(notes, body="second")

        await database.execute(
            "DELETE FROM personas WHERE account_id = ? AND profile = ?", (ACCOUNT, PROFILE)
        )

        assert await live_note_seqs(database) == set()
        assert await indexed_note_seqs(database) == set()
