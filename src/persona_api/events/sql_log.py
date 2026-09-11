"""The event log, as rows with no foreign keys.

The adapter for :class:`~persona_api.events.log.EventLog`. Everything about *why* it is
shaped this way is in that module's docstring; this one covers the two things that are
only visible in the SQL.

**The trim is part of the insert's transaction.** One ``DELETE`` immediately after the
``INSERT``, bounded by ``max(sequence) - max_entries``. Written against the sequence
rather than against a count, because ``count(*)`` on a growing table is a scan and this
runs on every write an assistant makes.

**Paging is by sequence, descending.** The cursor carries the last sequence seen, so a
walk cannot show an event twice or skip one even while events are being appended -- which
they are, constantly, since reading the log is itself the only operation here that does
not write one.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from persona_api.domain.cursors import decode_cursor, encode_cursor
from persona_api.domain.provenance import Source
from persona_api.events.log import EVENT_ID_PREFIX, MAX_ENTRIES, Event, EventAction
from persona_api.storage.times import from_column, to_column

if TYPE_CHECKING:
    import sqlite3

    from persona_api.core.clock import Clock
    from persona_api.storage.database import Database

COLUMNS = (
    "sequence, event_id, at, account_id, profile, action, subject, detail, source, asserted_by"
)

WRITABLE = "event_id, at, account_id, profile, action, subject, detail, source, asserted_by"


def new_event_id() -> str:
    """Return a fresh event id."""
    return f"{EVENT_ID_PREFIX}{uuid.uuid4().hex}"


class SqlEventLog:
    """An append-only table, trimmed to a bound."""

    def __init__(self, *, database: Database, clock: Clock, max_entries: int = MAX_ENTRIES) -> None:
        self._db = database
        self._clock = clock
        self._max_entries = max_entries

    # PLR0913: mirrors the port, which has this many fields because an entry has to be
    # self-describing -- nothing joins back to the rows it names, and they may be gone.
    def append(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        action: EventAction,
        account_id: str,
        profile: str,
        subject: str,
        detail: str = "",
        source: Source,
        asserted_by: str,
    ) -> Event:
        at = self._clock.now()
        event_id = new_event_id()
        cursor = connection.execute(
            f"INSERT INTO events ({WRITABLE}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
            (
                event_id,
                to_column(at),
                account_id,
                profile,
                action.value,
                subject,
                detail,
                source.value,
                asserted_by,
            ),
        )
        sequence = int(cursor.lastrowid or 0)

        # Trimmed in the same transaction as the insert, so the bound is a bound rather
        # than something a sweeper gets round to. Against max(sequence) rather than a
        # count(*), because this runs on every write and a count is a scan.
        connection.execute(
            "DELETE FROM events WHERE sequence <= (SELECT max(sequence) FROM events) - ?",
            (self._max_entries,),
        )

        return Event(
            event_id=event_id,
            sequence=sequence,
            at=at,
            account_id=account_id,
            profile=profile,
            action=action,
            subject=subject,
            detail=detail,
            source=source,
            asserted_by=asserted_by,
        )

    async def record(  # noqa: PLR0913
        self,
        *,
        action: EventAction,
        account_id: str,
        profile: str,
        subject: str,
        detail: str = "",
        source: Source,
        asserted_by: str,
    ) -> Event:
        return await self._db.transact(
            lambda connection: self.append(
                connection,
                action=action,
                account_id=account_id,
                profile=profile,
                subject=subject,
                detail=detail,
                source=source,
                asserted_by=asserted_by,
            )
        )

    async def recent(
        self, account_id: str, profile: str, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[Event], str | None]:
        after = int(decode_cursor(cursor)[0]) if cursor is not None else None

        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM events "  # noqa: S608
            "WHERE account_id = ? AND profile = ? AND (? IS NULL OR sequence < ?) "
            "ORDER BY sequence DESC LIMIT ?",
            (account_id, profile, after, after, limit + 1),
        )

        events = [_event_of(row) for row in rows[:limit]]
        if len(rows) <= limit:
            # One more row was asked for than will be returned, so its absence is how
            # we know this is the last page -- without a second count query that could
            # disagree with this one, and without the off-by-one where a full final
            # page hands back a cursor for an empty one.
            return events, None

        # `events` cannot be empty here: more rows came back than the limit, so the
        # slice above kept at least one. An `and events` guard would read as caution
        # and would be a branch no test could ever reach.
        last = events[-1]
        return events, encode_cursor(str(last.sequence), last.event_id)

    async def count(self) -> int:
        return await self._db.count("SELECT count(*) AS total FROM events")


def _event_of(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        sequence=row["sequence"],
        at=from_column(row["at"]),
        account_id=row["account_id"],
        profile=row["profile"],
        action=EventAction(row["action"]),
        subject=row["subject"],
        detail=row["detail"],
        source=Source(row["source"]),
        asserted_by=row["asserted_by"],
    )
