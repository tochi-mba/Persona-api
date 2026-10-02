"""The sweeper: bounded, checkpointing, and silent about what it destroyed."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from persona_api.core.config import LogFormat
from persona_api.core.logging import configure_logging
from persona_api.domain.erasure import ErasureMode, ErasurePolicy
from persona_api.domain.provenance import Source
from persona_api.memory.sweeper import SWEEP_BATCH, Sweeper
from tests.conftest import ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, set_field, write_note

if TYPE_CHECKING:
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.storage.database import Database
    from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.usefixtures("personas")

GRACE = ErasurePolicy(mode=ErasureMode.GRACE, grace_days=1)


def sweeper_for(
    database: Database, fields: FieldStore, notes: NoteStore, clock: FakeClock, batch: int
) -> Sweeper:
    return Sweeper(database=database, fields=fields, notes=notes, clock=clock, batch=batch)


async def forgotten(fields: FieldStore, notes: NoteStore, *, count: int) -> None:
    """``count`` fields and ``count`` notes, each forgotten under a one-day grace."""
    for index in range(count):
        await set_field(fields, key=f"key{index}")
        await fields.forget(
            ACCOUNT,
            PROFILE,
            f"key{index}",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            erasure=GRACE,
        )
        note = await write_note(notes, body=f"note number {index}")
        await notes.forget(
            ACCOUNT,
            PROFILE,
            note.note_id,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            erasure=GRACE,
        )


class TestOneSweep:
    async def test_nothing_due_is_nothing_destroyed(
        self, database: Database, fields: FieldStore, notes: NoteStore, clock: FakeClock
    ) -> None:
        await forgotten(fields, notes, count=2)

        assert await sweeper_for(database, fields, notes, clock, batch=10).sweep_once() == 0

    async def test_it_destroys_what_is_due_in_both_tables(
        self, database: Database, fields: FieldStore, notes: NoteStore, clock: FakeClock
    ) -> None:
        await forgotten(fields, notes, count=2)
        clock.advance(timedelta(days=1))

        assert await sweeper_for(database, fields, notes, clock, batch=10).sweep_once() == 4
        assert await fields.count(ACCOUNT, PROFILE, include_forgotten=True) == 0
        assert await notes.count(ACCOUNT, PROFILE, include_forgotten=True) == 0

    async def test_a_backlog_is_worked_off_a_batch_at_a_time(
        self, database: Database, fields: FieldStore, notes: NoteStore, clock: FakeClock
    ) -> None:
        """The bug, named: an unbounded sweep held the one database thread for a whole backlog."""
        await forgotten(fields, notes, count=5)
        clock.advance(timedelta(days=1))
        sweeper = sweeper_for(database, fields, notes, clock, batch=2)

        assert [await sweeper.sweep_once() for _ in range(4)] == [4, 4, 2, 0]

    def test_the_default_batch_is_bounded(self) -> None:
        assert 0 < SWEEP_BATCH <= 1_000

    async def test_it_logs_a_count_and_never_what_it_destroyed(
        self,
        database: Database,
        fields: FieldStore,
        notes: NoteStore,
        clock: FakeClock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        configure_logging(level="INFO", log_format=LogFormat.CONSOLE)
        await forgotten(fields, notes, count=1)
        clock.advance(timedelta(days=1))

        await sweeper_for(database, fields, notes, clock, batch=10).sweep_once()

        output = capsys.readouterr().out
        assert "forgotten_rows_destroyed" in output
        assert "key0" not in output
        assert "note number" not in output
