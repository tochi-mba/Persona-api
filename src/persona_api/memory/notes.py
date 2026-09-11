"""Free text, as rows, with its search index kept in step by hand.

The mirror of :mod:`persona_api.memory.fields`, and that module's docstring covers the
decisions the two share -- the explicit index, the docid counter, caps enforced inside
the transaction. Three things differ, and each follows from a note being an event in time
rather than a piece of current state.

**A note accrues; a field is replaced.** Writing is ``POST`` and mints a new id, so two
notes saying the same thing at different times are two notes. There is no idempotent
upsert here, because there is no key to be idempotent on.

**Revising is a PATCH of the parts you name.** Body, kind and pin can each be changed
independently, and anything left out is left alone. A caller that had to resend the body
to change the pin would be one race away from overwriting an edit it never saw.

**Ordering and range filters are on ``created_at``**, not ``updated_at``. When a note
happened is what you ask about; when it was last edited is not. Fields go the other way,
for the same reason in reverse. See :mod:`persona_api.memory.filters`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona_api.domain.cursors import decode_cursor, encode_cursor
from persona_api.domain.errors import LimitExceededError, NoteNotFoundError
from persona_api.domain.notes import Note, NoteKind, new_note_id, normalize_body
from persona_api.domain.provenance import Source
from persona_api.domain.secrets import refuse_if_credential
from persona_api.events.log import EventAction
from persona_api.memory.pagination import Page
from persona_api.memory.search import to_match_query
from persona_api.storage.times import from_column, from_column_optional, to_column

if TYPE_CHECKING:
    import sqlite3

    from persona_api.core.clock import Clock
    from persona_api.events.log import EventLog
    from persona_api.memory.filters import NoteFilters
    from persona_api.storage.database import Database

COLUMNS = (
    "note_id, account_id, profile, seq, body, kind, source, asserted_by, "
    "pinned, revision, created_at, updated_at, forgotten_at"
)


class NoteStore:
    """Notes in one table, their searchable text in another."""

    # PLR0913: see FieldStore.__init__ -- the same six collaborators, for the same
    # reason.
    def __init__(  # noqa: PLR0913
        self,
        *,
        database: Database,
        clock: Clock,
        events: EventLog,
        max_body_chars: int,
        max_notes: int,
        max_pinned: int,
    ) -> None:
        self._db = database
        self._clock = clock
        self._events = events
        self._max_body_chars = max_body_chars
        self._max_notes = max_notes
        self._max_pinned = max_pinned

    # PLR0913: the note plus its provenance. Every one is required for the row.
    async def write(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        profile: str,
        body: str,
        kind: NoteKind,
        source: Source,
        asserted_by: str,
        pinned: bool = False,
    ) -> Note:
        """Record one note.

        Raises:
            CredentialRefusedError: if the body looks like a credential.
            InvalidNoteError: the body is blank or over the configured length.
            LimitExceededError: the persona is at its note or pinned cap.
        """
        text = normalize_body(body, limit=self._max_body_chars)
        refuse_if_credential(text)
        now = self._clock.now()
        note_id = new_note_id()

        def do_write(connection: sqlite3.Connection) -> Note:
            self._check_caps(connection, account_id, profile, adding_pinned=pinned, adding=True)
            seq = _next_seq(connection)
            connection.execute(
                f"INSERT INTO notes ({COLUMNS}) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    account_id,
                    profile,
                    seq,
                    text,
                    kind.value,
                    source.value,
                    asserted_by,
                    int(pinned),
                    1,
                    to_column(now),
                    to_column(now),
                    None,
                ),
            )
            self._index(connection, seq, text)
            self._events.append(
                connection,
                action=EventAction.NOTE_WRITTEN,
                account_id=account_id,
                profile=profile,
                subject=note_id,
                detail=kind.value,
                source=source,
                asserted_by=asserted_by,
            )
            return _note_of(_row(connection, note_id))

        return await self._db.transact(do_write)

    async def get(
        self, account_id: str, profile: str, note_id: str, *, include_forgotten: bool = False
    ) -> Note | None:
        """One note by id, or ``None`` -- which covers "not there" and "not yours"."""
        row = await self._db.fetch_one(
            f"SELECT {COLUMNS} FROM notes "  # noqa: S608
            "WHERE account_id = ? AND profile = ? AND note_id = ? "
            "AND (? OR forgotten_at IS NULL)",
            (account_id, profile, note_id, include_forgotten),
        )
        return None if row is None else _note_of(row)

    async def list_for_persona(
        self,
        account_id: str,
        profile: str,
        *,
        filters: NoteFilters,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Note]:
        """One page of this persona's notes, newest first."""
        where, parameters = _conditions(account_id, profile, filters)

        if cursor is not None:
            sort_key, row_id = decode_cursor(cursor)
            where.append("(created_at, note_id) < (?, ?)")
            parameters.extend([sort_key, row_id])

        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM notes WHERE {' AND '.join(where)} "  # noqa: S608
            "ORDER BY created_at DESC, note_id DESC LIMIT ?",
            (*parameters, limit + 1),
        )
        return _page(rows, limit)

    async def pinned(self, account_id: str, profile: str) -> list[Note]:
        """Every pinned note, for the identity block. Bounded by the pinned cap."""
        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM notes "  # noqa: S608
            "WHERE account_id = ? AND profile = ? AND pinned = 1 AND forgotten_at IS NULL "
            "ORDER BY created_at DESC, note_id DESC",
            (account_id, profile),
        )
        return [_note_of(row) for row in rows]

    async def search(
        self, account_id: str, *, query: str, profile: str | None = None, limit: int
    ) -> list[Note]:
        """Full-text search, ranked by ``bm25`` within this index.

        The account filter is in the ``JOIN`` rather than the ``MATCH`` -- see
        :meth:`persona_api.memory.fields.FieldStore.search` for why that is the one
        place isolation here is not structural, and which test exists for it.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_prefixed(COLUMNS, 'n')} FROM notes_fts "  # noqa: S608
            "JOIN notes n ON n.seq = notes_fts.rowid "
            "WHERE notes_fts MATCH ? AND n.account_id = ? "
            "AND (? IS NULL OR n.profile = ?) AND n.forgotten_at IS NULL "
            "ORDER BY bm25(notes_fts) LIMIT ?",
            (to_match_query(query), account_id, profile, profile, limit),
        )
        return [_note_of(row) for row in rows]

    # PLR0913: a PATCH of three independently optional parts, plus the provenance of
    # whoever is making the change.
    async def revise(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        profile: str,
        note_id: str,
        source: Source,
        asserted_by: str,
        body: str | None = None,
        kind: NoteKind | None = None,
        pinned: bool | None = None,
    ) -> Note:
        """Change the parts of a note that are named, leaving the rest alone.

        Raises:
            NoteNotFoundError: no live note by that id in this persona.
            CredentialRefusedError: if a new body looks like a credential.
            LimitExceededError: pinning would take the persona past its pinned cap.
        """
        text = None if body is None else normalize_body(body, limit=self._max_body_chars)
        if text is not None:
            refuse_if_credential(text)
        now = self._clock.now()

        def write(connection: sqlite3.Connection) -> Note:
            existing = connection.execute(
                f"SELECT {COLUMNS} FROM notes "  # noqa: S608
                "WHERE account_id = ? AND profile = ? AND note_id = ? AND forgotten_at IS NULL",
                (account_id, profile, note_id),
            ).fetchone()
            if existing is None:
                msg = "no note by that id"
                raise NoteNotFoundError(msg)

            new_body = existing["body"] if text is None else text
            new_kind = existing["kind"] if kind is None else kind.value
            new_pinned = bool(existing["pinned"]) if pinned is None else pinned

            if (
                new_body == existing["body"]
                and new_kind == existing["kind"]
                and new_pinned == bool(existing["pinned"])
            ):
                # Same reason a field's unchanged PUT does not bump a revision: the
                # number means "how often this changed", not "how often it was written".
                return _note_of(existing)

            if new_pinned and not existing["pinned"]:
                self._check_caps(connection, account_id, profile, adding_pinned=True, adding=False)

            connection.execute(
                "UPDATE notes SET body = ?, kind = ?, pinned = ?, source = ?, asserted_by = ?,"
                " revision = revision + 1, updated_at = ? WHERE note_id = ?",
                (
                    new_body,
                    new_kind,
                    int(new_pinned),
                    source.value,
                    asserted_by,
                    to_column(now),
                    note_id,
                ),
            )
            self._unindex(connection, existing["seq"])
            self._index(connection, existing["seq"], new_body)
            self._events.append(
                connection,
                action=EventAction.NOTE_REVISED,
                account_id=account_id,
                profile=profile,
                subject=note_id,
                detail=f"revision {existing['revision'] + 1}",
                source=source,
                asserted_by=asserted_by,
            )
            return _note_of(_row(connection, note_id))

        return await self._db.transact(write)

    async def forget(
        self, account_id: str, profile: str, note_id: str, *, source: Source, asserted_by: str
    ) -> None:
        """Tombstone one note. Reversible with ``include_forgotten``.

        Raises:
            NoteNotFoundError: no live note by that id in this persona. Identical to the
                answer for another account's note.
        """
        now = self._clock.now()

        def write(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT seq FROM notes "
                "WHERE account_id = ? AND profile = ? AND note_id = ? AND forgotten_at IS NULL",
                (account_id, profile, note_id),
            ).fetchone()
            if existing is None:
                msg = "no note by that id"
                raise NoteNotFoundError(msg)

            connection.execute(
                "UPDATE notes SET forgotten_at = ?, updated_at = ? WHERE note_id = ?",
                (to_column(now), to_column(now), note_id),
            )
            # Out of the index as well as out of the listing: a forgotten note that
            # still came back in recall would make the tombstone cosmetic.
            self._unindex(connection, existing["seq"])
            self._events.append(
                connection,
                action=EventAction.NOTE_FORGOTTEN,
                account_id=account_id,
                profile=profile,
                subject=note_id,
                source=source,
                asserted_by=asserted_by,
            )

        await self._db.transact(write)

    async def count(self, account_id: str, profile: str, *, include_forgotten: bool = False) -> int:
        """How many notes this persona holds."""
        return await self._db.count(
            "SELECT count(*) AS total FROM notes "
            "WHERE account_id = ? AND profile = ? AND (? OR forgotten_at IS NULL)",
            (account_id, profile, include_forgotten),
        )

    def _check_caps(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        profile: str,
        *,
        adding_pinned: bool,
        adding: bool,
    ) -> None:
        """Refuse a write that would pass a cap, inside the caller's transaction."""
        if adding:
            held = connection.execute(
                "SELECT count(*) AS total FROM notes "
                "WHERE account_id = ? AND profile = ? AND forgotten_at IS NULL",
                (account_id, profile),
            ).fetchone()["total"]
            if held >= self._max_notes:
                msg = f"at most {self._max_notes} notes per persona"
                raise LimitExceededError(msg)

        if adding_pinned:
            held = connection.execute(
                "SELECT count(*) AS total FROM notes WHERE account_id = ? AND profile = ?"
                " AND pinned = 1 AND forgotten_at IS NULL",
                (account_id, profile),
            ).fetchone()["total"]
            if held >= self._max_pinned:
                msg = f"at most {self._max_pinned} pinned notes per persona"
                raise LimitExceededError(msg)

    def _index(self, connection: sqlite3.Connection, seq: int, text: str) -> None:
        """Write one row's searchable text. Called from every write path."""
        connection.execute("INSERT INTO notes_fts (rowid, text) VALUES (?, ?)", (seq, text))

    def _unindex(self, connection: sqlite3.Connection, seq: int) -> None:
        """Remove one row's searchable text, by the docid the row carries."""
        connection.execute("DELETE FROM notes_fts WHERE rowid = ?", (seq,))


def _next_seq(connection: sqlite3.Connection) -> int:
    """Allocate a full-text docid. Monotonic, and never reused. See ADR-0008."""
    row = connection.execute(
        "UPDATE fts_sequence SET next = next + 1 WHERE name = 'notes' RETURNING next"
    ).fetchone()
    return int(row["next"])


def _row(connection: sqlite3.Connection, note_id: str) -> sqlite3.Row:
    """Re-read a note we just wrote, so the caller gets the row as stored.

    Returned rather than reconstructed from the values passed in, because the row is
    what the next reader will see -- and a mapper that drifted from the DDL would then
    be caught here rather than months later.
    """
    row: sqlite3.Row = connection.execute(
        f"SELECT {COLUMNS} FROM notes WHERE note_id = ?",  # noqa: S608
        (note_id,),
    ).fetchone()
    return row


def _prefixed(columns: str, alias: str) -> str:
    """Qualify a column list, for the joins where two tables share a name."""
    return ", ".join(f"{alias}.{column.strip()}" for column in columns.split(","))


def _conditions(account_id: str, profile: str, filters: NoteFilters) -> tuple[list[str], list[Any]]:
    """Turn filters into a WHERE clause, in the order the index expects them."""
    where = ["account_id = ?", "profile = ?"]
    parameters: list[Any] = [account_id, profile]

    if not filters.include_forgotten:
        where.append("forgotten_at IS NULL")
    if filters.kind is not None:
        where.append("kind = ?")
        parameters.append(filters.kind.value)
    if filters.source is not None:
        where.append("source = ?")
        parameters.append(filters.source.value)
    if filters.pinned is not None:
        where.append("pinned = ?")
        parameters.append(int(filters.pinned))
    if filters.since is not None:
        where.append("created_at >= ?")
        parameters.append(to_column(filters.since))
    if filters.until is not None:
        where.append("created_at < ?")
        parameters.append(to_column(filters.until))

    return where, parameters


def _page(rows: list[sqlite3.Row], limit: int) -> Page[Note]:
    notes = [_note_of(row) for row in rows[:limit]]
    if len(rows) <= limit:
        return Page(items=notes, next_cursor=None)
    last = notes[-1]
    return Page(items=notes, next_cursor=encode_cursor(to_column(last.created_at), last.note_id))


def _note_of(row: sqlite3.Row) -> Note:
    return Note(
        note_id=row["note_id"],
        account_id=row["account_id"],
        profile=row["profile"],
        body=row["body"],
        kind=NoteKind(row["kind"]),
        source=Source(row["source"]),
        asserted_by=row["asserted_by"],
        pinned=bool(row["pinned"]),
        revision=row["revision"],
        created_at=from_column(row["created_at"]),
        updated_at=from_column(row["updated_at"]),
        forgotten_at=from_column_optional(row["forgotten_at"]),
    )
