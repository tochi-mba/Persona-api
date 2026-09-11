"""What changed in a persona, and when.

An append-only record, readable only by the account that owns the persona it describes.
Four decisions shape it, and three of them are inherited from keyring's audit log for
reasons that turn out to apply here too.

## It never holds a field value or a note body

Only the key or the note id, plus a short human-readable ``detail``. An event log is read
more often, by more tools, and kept for longer than the rows it describes -- and the rows
it describes are a person's own words about themselves. "field.revised voice" is enough
to answer what changed; putting the old and new value in would make this table a second,
permanent, un-forgettable copy of the persona that ``forget`` cannot reach.

That is also why it is safe for this to be the one table with no tombstones.

## No foreign keys, on purpose

Deleting a persona must not delete the record that it was deleted. ``account_id`` and
``profile`` are opaque and their rows may already be gone, so an entry has to be
self-describing: what it says is all that will be left.

## Ordered by sequence, not by time

The clock is injectable and two events recorded in the same tick share a timestamp. For
"newest first" to be an order at all rather than an approximation of one, it has to be
insertion order -- which is what the autoincrementing ``sequence`` column is.

## Capped, and trimmed in the same transaction as the insert

A table driven by write volume grows without anybody deciding it should, and an assistant
may write on every turn. Trimming inside the insert's own transaction makes the bound a
bound, rather than something a sweeper gets round to and stops doing the day it crashes.

## Two ways to write one

:meth:`EventLog.append` is **synchronous** and takes a connection, so a store can record
an event inside the very transaction that made the change. That is the one that matters:
a write and its event either both happen or neither does, and a crash between them cannot
leave a change nothing recorded.

:meth:`EventLog.record` opens its own transaction, for callers that are not already
inside one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from persona_api.domain.provenance import Source

MAX_ENTRIES = 10_000
"""Oldest entries are dropped past this. See the module docstring."""

EVENT_ID_PREFIX = "evt_"


class EventAction(StrEnum):
    """What was done. A closed set, so the log can be filtered reliably.

    Closed for the same reason ``NoteKind`` is: a typo'd free string is an event that
    can never be filtered for again, and nobody notices until they go looking.

    ``<noun>.<verb>``, so a prefix match answers "everything that happened to fields".
    """

    PERSONA_CREATED = "persona.created"
    PERSONA_UPDATED = "persona.updated"
    PERSONA_DELETED = "persona.deleted"

    FIELD_SET = "field.set"
    """A key that had no live row now has one."""

    FIELD_REVISED = "field.revised"
    """An existing key's value, description or pin changed."""

    FIELD_FORGOTTEN = "field.forgotten"

    NOTE_WRITTEN = "note.written"
    NOTE_REVISED = "note.revised"
    NOTE_FORGOTTEN = "note.forgotten"


@dataclass(frozen=True, slots=True)
class Event:
    """One recorded change."""

    event_id: str
    sequence: int
    """Insertion order. The ordering, because ``at`` is not one -- see the module docstring."""

    at: datetime
    account_id: str
    profile: str
    action: EventAction

    subject: str
    """The field key or the note id. Never a value and never a body."""

    detail: str
    """A short human-readable summary. Never a value and never a body."""

    source: Source
    """Who the writer said asserted the change. A claim, exactly as it is on a field."""

    asserted_by: str
    """The ``aud`` of the verified token that made the change. Server-derived."""


@runtime_checkable
class EventLog(Protocol):
    """Records changes and reads them back.

    A port rather than a concrete class, so a store can be handed a fake that satisfies
    it -- and so a change to this signature fails the fakes' type check rather than
    silently diverging from them.
    """

    # PLR0913: an event has this many fields, and every one of them is required for the
    # entry to be self-describing. See the module docstring on why it has to be.
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
        """Record a change inside the caller's own transaction.

        Synchronous, and takes the connection, precisely so that a store can call it
        between its own statements: the change and its event then commit together or
        not at all.
        """
        ...

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
        """Record a change in a transaction of its own."""
        ...

    async def recent(
        self, account_id: str, profile: str, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[Event], str | None]:
        """Read one persona's events, newest first, with the cursor for the next page.

        Scoped to an account and a profile, and not optionally: there is no call here
        that could read another account's history, which is what makes isolation
        structural rather than a check somebody has to remember.
        """
        ...

    async def count(self) -> int:
        """How many entries are held, across every account. For ``/healthy``."""
        ...
