"""Where a grace period ends.

A field or note forgotten under ``persona.erasure_mode = grace`` carries the instant it may
be destroyed, written by the request that forgot it. This destroys whatever is due, and
that is all it does: it reads no settings and presents no token, because the decision was
taken -- and written down -- while somebody's token was in hand. See
:mod:`persona_api.domain.erasure`.

Three properties, each with a test:

**Bounded.** At most ``batch`` fields and ``batch`` notes per sweep, oldest-due first. A
backlog -- a deployment that was down for a month, somebody who forgot five thousand notes
on one day -- is worked off over several sweeps rather than holding the single database
thread for as long as it takes. Nothing is lost by that: a row that waits one more interval
is a row that is still hidden from every read.

**Two steps, and the second is not housekeeping.** The rows, the values the event log
kept about them and their words in the search index go in one transaction per table; then,
once, the write-ahead log is truncated. Without the checkpoint the destroyed text is still
on disk beside the database, findable with ``grep`` -- user-api measured that before this
family settled on it.

**It never says what it destroyed.** It logs a count. A log line naming the key of the
field it just erased would be a small copy of exactly what somebody asked to be rid of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.core.logging import get_logger

if TYPE_CHECKING:
    from persona_api.core.clock import Clock
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.storage.database import Database

logger = get_logger(__name__)

SWEEP_BATCH = 500
"""Fields, and separately notes, destroyed per sweep at most. See the module docstring."""


class Sweeper:
    """Destroys forgotten rows whose grace period has run out."""

    def __init__(
        self,
        *,
        database: Database,
        fields: FieldStore,
        notes: NoteStore,
        clock: Clock,
        batch: int = SWEEP_BATCH,
    ) -> None:
        self._db = database
        self._fields = fields
        self._notes = notes
        self._clock = clock
        self._batch = batch

    async def sweep_once(self) -> int:
        """Destroy one batch of what is due. Returns how many rows went."""
        now = self._clock.now()
        purged = await self._fields.purge_due(now=now, limit=self._batch)
        purged += await self._notes.purge_due(now=now, limit=self._batch)
        if purged:
            # Once, after both tables: this is the step that takes the bytes out of the
            # write-ahead log, and it cannot run inside either transaction.
            await self._db.checkpoint_truncate()
            logger.info("forgotten_rows_destroyed", destroyed=purged)
        return purged


__all__ = ["SWEEP_BATCH", "Sweeper"]
