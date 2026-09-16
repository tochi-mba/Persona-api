"""Structured attributes, as rows, with their search index kept in step by hand.

## The index is written here, explicitly, inside the same transaction as the row

Not external-content FTS5 tables, and not triggers.

External content requires issuing ``'delete'`` commands carrying the **old** values on
every update. Miss one and the index silently disagrees with the table -- a well-known
footgun rather than a hypothetical, and the failure is invisible until somebody searches
for something that is definitely there and does not find it.

Triggers would have to render a JSON value to searchable text in SQL, which means
reimplementing :func:`~persona_api.domain.fields.flatten_value` in a language that cannot
be unit-tested.

So every write path goes through one private ``_index`` helper. That choice creates its
own bug class -- a write path that forgets to call it -- which is why index integrity has
its own test class asserting the table and the index agree after create, revise, forget
and persona delete.

## The docid comes from a counter, not from the row

``fields.seq`` is allocated from the ``fts_sequence`` table and used as the FTS5 rowid.
Two reasons, both in ``docs/adr/0008-sqlite.md``: ``VACUUM`` renumbers implicit rowids,
and ``VACUUM INTO`` is what an operator is told to back up with; and a counter that only
goes up cannot reuse an id freed by a delete.

## Caps are enforced inside the transaction that writes

Never by a caller that counts first. A count taken before a write goes stale between the
two, and two concurrent writes both pass it. There is an ``asyncio.gather`` test per cap
that would find it if that ever stopped being true.

## set() is idempotent, and knows when nothing changed

The key is in the path, so create-and-update are one operation -- a model retrying a call
must not get two fields. An unchanged write is recognised as one and does **not** bump the
revision, which is what makes ``revision`` mean "how many times this actually changed"
rather than "how many times somebody called PUT".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from persona_api.domain.cursors import decode_cursor, encode_cursor
from persona_api.domain.errors import FieldNotFoundError, LimitExceededError
from persona_api.domain.fields import (
    Field,
    ValueType,
    derive_value_type,
    flatten_value,
    new_field_id,
    normalize_description,
    normalize_key,
    validate_value,
)
from persona_api.domain.provenance import Source
from persona_api.domain.secrets import refuse_if_credential
from persona_api.events.log import EventAction
from persona_api.memory.pagination import Page
from persona_api.memory.search import to_match_query
from persona_api.storage.times import from_column, from_column_optional, to_column

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from persona_api.core.clock import Clock
    from persona_api.domain.fields import ValueLimits
    from persona_api.events.log import EventLog
    from persona_api.memory.filters import FieldFilters
    from persona_api.storage.database import Database

COLUMNS = (
    "account_id, profile, key, field_id, seq, description, value_json, value_type, "
    "source, asserted_by, pinned, revision, created_at, updated_at, forgotten_at"
)


class FieldSummary:
    """One line of ``/schema``: what a key is for, without what it holds.

    A plain class rather than a dataclass because it is three attributes and a
    docstring; what matters is the absence of a ``value``. ``/schema`` is the
    anti-sprawl endpoint and has to be cheap enough that an assistant calls it *before*
    inventing a key, which it will not be if it carries every value in the persona.
    """

    __slots__ = ("description", "key", "pinned", "updated_at", "value_type")

    def __init__(
        self,
        *,
        key: str,
        description: str,
        value_type: ValueType,
        pinned: bool,
        updated_at: datetime,
    ) -> None:
        self.key = key
        self.description = description
        self.value_type = value_type
        self.pinned = pinned
        self.updated_at = updated_at


class FieldStore:
    """Fields in one table, their searchable text in another."""

    # PLR0913: six collaborators, and each is a different kind of thing -- where rows
    # go, what time it is, where events go, and three numbers a deployment tunes.
    # Bundling them would add a type whose only job is to be unpacked one line later.
    def __init__(  # noqa: PLR0913
        self,
        *,
        database: Database,
        clock: Clock,
        events: EventLog,
        limits: ValueLimits,
        max_fields: int,
        max_pinned: int,
    ) -> None:
        self._db = database
        self._clock = clock
        self._events = events
        self._limits = limits
        self._max_fields = max_fields
        self._max_pinned = max_pinned

    # PLR0913: the arguments are the field itself plus its provenance, and every one is
    # required for the row to be complete. A request object here would be unpacked one
    # line later.
    async def set(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        profile: str,
        key: str,
        description: str,
        value: object,
        source: Source,
        asserted_by: str,
        pinned: bool | None = None,
        max_pinned: int | None = None,
    ) -> Field:
        """Create or replace one field. Idempotent on the key.

        ``max_pinned`` is this write's pin ceiling. When omitted the constructor value
        stands, so existing callers keep working; when passed it wins for this write, so
        two concurrent requests for two accounts can have different ceilings.

        Raises:
            CredentialRefusedError: if the value or the description looks like a
                credential. There is no setting that disables this.
            InvalidFieldKeyError: the key or the description cannot be stored.
            InvalidFieldValueError: the value exceeds a limit, naming which.
            LimitExceededError: the persona is at its field or pinned cap.
        """
        normalized = normalize_key(key)
        described = normalize_description(description)
        value_json = validate_value(value, limits=self._limits)
        value_type = derive_value_type(value)

        # Both halves of what a caller supplies, because a key pasted into a
        # description is as disclosed as one pasted into a value.
        refuse_if_credential(value_json)
        refuse_if_credential(described)

        now = self._clock.now()
        searchable = f"{normalized} {described} {flatten_value(value)}"
        pinned_cap = self._max_pinned if max_pinned is None else max_pinned

        def write(connection: sqlite3.Connection) -> Field:
            existing = connection.execute(
                f"SELECT {COLUMNS} FROM fields WHERE account_id = ? AND profile = ? AND key = ?",  # noqa: S608
                (account_id, profile, normalized),
            ).fetchone()

            if existing is None:
                return self._insert(
                    connection,
                    account_id=account_id,
                    profile=profile,
                    key=normalized,
                    description=described,
                    value_json=value_json,
                    value_type=value_type,
                    source=source,
                    asserted_by=asserted_by,
                    pinned=bool(pinned),
                    now=now,
                    searchable=searchable,
                    max_pinned=pinned_cap,
                )

            return self._revise(
                connection,
                existing=existing,
                description=described,
                value_json=value_json,
                value_type=value_type,
                source=source,
                asserted_by=asserted_by,
                pinned=pinned,
                now=now,
                searchable=searchable,
                max_pinned=pinned_cap,
            )

        return await self._db.transact(write)

    async def get(
        self, account_id: str, profile: str, key: str, *, include_forgotten: bool = False
    ) -> Field | None:
        """One field by key, or ``None``.

        ``None`` covers both "no such key" and "somebody else's persona", because a
        distinguishable answer would say that another account has a field by that name.
        """
        normalized = normalize_key(key)
        row = await self._db.fetch_one(
            f"SELECT {COLUMNS} FROM fields "  # noqa: S608
            "WHERE account_id = ? AND profile = ? AND key = ? "
            "AND (? OR forgotten_at IS NULL)",
            (account_id, profile, normalized, include_forgotten),
        )
        return None if row is None else _field_of(row)

    async def list_for_persona(
        self,
        account_id: str,
        profile: str,
        *,
        filters: FieldFilters,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Field]:
        """One page of this persona's fields, most recently changed first."""
        where, parameters = _conditions(account_id, profile, filters)

        if cursor is not None:
            sort_key, row_id = decode_cursor(cursor)
            where.append("(updated_at, key) < (?, ?)")
            parameters.extend([sort_key, row_id])

        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM fields WHERE {' AND '.join(where)} "  # noqa: S608
            "ORDER BY updated_at DESC, key DESC LIMIT ?",
            (*parameters, limit + 1),
        )
        return _page(rows, limit)

    async def pinned(self, account_id: str, profile: str) -> list[Field]:
        """Every pinned field, for the identity block. Bounded by the pinned cap."""
        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM fields "  # noqa: S608
            "WHERE account_id = ? AND profile = ? AND pinned = 1 AND forgotten_at IS NULL "
            "ORDER BY updated_at DESC, key DESC",
            (account_id, profile),
        )
        return [_field_of(row) for row in rows]

    async def schema(self, account_id: str, profile: str) -> list[FieldSummary]:
        """Keys, descriptions and types. **No values.**

        The anti-sprawl endpoint. Cheap enough that an assistant can call it before
        inventing a key, which is the whole mechanism by which ``voice`` does not become
        ``voice``, ``tone_of_voice`` and ``speaking_style``.
        """
        rows = await self._db.fetch_all(
            "SELECT key, description, value_type, pinned, updated_at FROM fields "
            "WHERE account_id = ? AND profile = ? AND forgotten_at IS NULL "
            "ORDER BY key",
            (account_id, profile),
        )
        return [
            FieldSummary(
                key=row["key"],
                description=row["description"],
                value_type=ValueType(row["value_type"]),
                pinned=bool(row["pinned"]),
                updated_at=from_column(row["updated_at"]),
            )
            for row in rows
        ]

    async def search(
        self, account_id: str, *, query: str, profile: str | None = None, limit: int
    ) -> list[Field]:
        """Full-text search, ranked by ``bm25`` within this index.

        The account filter lives in the ``JOIN``, not in the ``MATCH``: the FTS index is
        shared across every account by construction, so this is the one place in the
        service where isolation is not structural. ``TestSearchIsolation`` exists
        specifically for it.

        ``bm25`` also ranks against the whole corpus rather than one account's. At this
        scale that is irrelevant to result quality and it leaks nothing -- the rows are
        filtered before they are returned -- but it is worth knowing so nobody later
        mistakes it for a leak.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_prefixed(COLUMNS, 'f')} FROM fields_fts "  # noqa: S608
            "JOIN fields f ON f.seq = fields_fts.rowid "
            "WHERE fields_fts MATCH ? AND f.account_id = ? "
            "AND (? IS NULL OR f.profile = ?) AND f.forgotten_at IS NULL "
            "ORDER BY bm25(fields_fts) LIMIT ?",
            (to_match_query(query), account_id, profile, profile, limit),
        )
        return [_field_of(row) for row in rows]

    async def forget(
        self, account_id: str, profile: str, key: str, *, source: Source, asserted_by: str
    ) -> None:
        """Tombstone one field. Reversible with ``include_forgotten``.

        Raises:
            FieldNotFoundError: no live field by that key in this persona. Identical to
                the answer for another account's field.
        """
        normalized = normalize_key(key)
        now = self._clock.now()

        def write(connection: sqlite3.Connection) -> None:
            forgotten = connection.execute(
                "UPDATE fields SET forgotten_at = ?, updated_at = ? "
                "WHERE account_id = ? AND profile = ? AND key = ? AND forgotten_at IS NULL",
                (to_column(now), to_column(now), account_id, profile, normalized),
            ).rowcount
            if not forgotten:
                msg = "no field by that key"
                raise FieldNotFoundError(msg)

            # The index entry goes even though the row stays. A forgotten field must not
            # come back in recall -- that is what forgetting means -- and leaving it
            # indexed would make the tombstone cosmetic.
            self._unindex(connection, account_id, profile, normalized)
            self._events.append(
                connection,
                action=EventAction.FIELD_FORGOTTEN,
                account_id=account_id,
                profile=profile,
                subject=normalized,
                source=source,
                asserted_by=asserted_by,
            )

        await self._db.transact(write)

    async def count(self, account_id: str, profile: str, *, include_forgotten: bool = False) -> int:
        """How many fields this persona holds."""
        return await self._db.count(
            "SELECT count(*) AS total FROM fields "
            "WHERE account_id = ? AND profile = ? AND (? OR forgotten_at IS NULL)",
            (account_id, profile, include_forgotten),
        )

    # -- writing ------------------------------------------------------------------------

    def _insert(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        profile: str,
        key: str,
        description: str,
        value_json: str,
        value_type: ValueType,
        source: Source,
        asserted_by: str,
        pinned: bool,
        now: datetime,
        searchable: str,
        max_pinned: int,
    ) -> Field:
        self._check_caps(
            connection, account_id, profile, adding_pinned=pinned, max_pinned=max_pinned
        )
        seq = _next_seq(connection, "fields")
        field_id = new_field_id()

        connection.execute(
            f"INSERT INTO fields ({COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                profile,
                key,
                field_id,
                seq,
                description,
                value_json,
                value_type.value,
                source.value,
                asserted_by,
                int(pinned),
                1,
                to_column(now),
                to_column(now),
                None,
            ),
        )
        self._index(connection, seq, searchable)
        self._events.append(
            connection,
            action=EventAction.FIELD_SET,
            account_id=account_id,
            profile=profile,
            subject=key,
            detail=f"set as {value_type.value}",
            source=source,
            asserted_by=asserted_by,
        )
        return _field_of(
            connection.execute(
                f"SELECT {COLUMNS} FROM fields WHERE field_id = ?",  # noqa: S608
                (field_id,),
            ).fetchone()
        )

    def _revise(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        existing: sqlite3.Row,
        description: str,
        value_json: str,
        value_type: ValueType,
        source: Source,
        asserted_by: str,
        pinned: bool | None,
        now: datetime,
        searchable: str,
        max_pinned: int,
    ) -> Field:
        account_id = existing["account_id"]
        profile = existing["profile"]
        key = existing["key"]
        was_forgotten = existing["forgotten_at"] is not None
        will_be_pinned = bool(existing["pinned"]) if pinned is None else pinned

        unchanged = (
            existing["value_json"] == value_json
            and existing["description"] == description
            and bool(existing["pinned"]) == will_be_pinned
            and not was_forgotten
        )
        if unchanged:
            # A model retrying a call, or writing what it already wrote, must not make
            # `revision` a count of how often PUT was called. The canonical JSON
            # serialization is what makes this comparison reliable.
            return _field_of(existing)

        if was_forgotten or (will_be_pinned and not existing["pinned"]):
            # Reviving a tombstone adds a live row, and pinning adds a pinned one.
            # Either can take the persona past a cap that a plain edit never could.
            self._check_caps(
                connection,
                account_id,
                profile,
                adding_pinned=will_be_pinned and not existing["pinned"],
                reviving=was_forgotten,
                max_pinned=max_pinned,
            )

        connection.execute(
            "UPDATE fields SET description = ?, value_json = ?, value_type = ?, source = ?,"
            " asserted_by = ?, pinned = ?, revision = revision + 1, updated_at = ?,"
            " forgotten_at = NULL"
            " WHERE account_id = ? AND profile = ? AND key = ?",
            (
                description,
                value_json,
                value_type.value,
                source.value,
                asserted_by,
                int(will_be_pinned),
                to_column(now),
                account_id,
                profile,
                key,
            ),
        )
        self._unindex(connection, account_id, profile, key)
        self._index(connection, existing["seq"], searchable)
        self._events.append(
            connection,
            action=EventAction.FIELD_SET if was_forgotten else EventAction.FIELD_REVISED,
            account_id=account_id,
            profile=profile,
            subject=key,
            detail=f"revision {existing['revision'] + 1}",
            source=source,
            asserted_by=asserted_by,
        )
        return _field_of(
            connection.execute(
                f"SELECT {COLUMNS} FROM fields "  # noqa: S608
                "WHERE account_id = ? AND profile = ? AND key = ?",
                (account_id, profile, key),
            ).fetchone()
        )

    # PLR0913: connection, identity, and the three caps this write is asking for.
    def _check_caps(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        account_id: str,
        profile: str,
        *,
        adding_pinned: bool,
        max_pinned: int,
        reviving: bool = True,
    ) -> None:
        """Refuse a write that would take the persona past a cap.

        Inside the caller's transaction, which is the entire point: a count taken before
        the write goes stale between the two, and two concurrent writes both pass it.
        """
        if reviving:
            live = connection.execute(
                "SELECT count(*) AS total FROM fields "
                "WHERE account_id = ? AND profile = ? AND forgotten_at IS NULL",
                (account_id, profile),
            ).fetchone()["total"]
            if live >= self._max_fields:
                msg = f"at most {self._max_fields} fields per persona"
                raise LimitExceededError(msg)

        if adding_pinned:
            held = connection.execute(
                "SELECT count(*) AS total FROM fields WHERE account_id = ? AND profile = ?"
                " AND pinned = 1 AND forgotten_at IS NULL",
                (account_id, profile),
            ).fetchone()["total"]
            if held >= max_pinned:
                # Pinned is a token budget, not a preference: every pinned entry goes
                # into the assistant's prompt on every turn.
                msg = f"at most {max_pinned} pinned fields per persona"
                raise LimitExceededError(msg)

    def _index(self, connection: sqlite3.Connection, seq: int, text: str) -> None:
        """Write one row's searchable projection. Called from every write path."""
        connection.execute("INSERT INTO fields_fts (rowid, text) VALUES (?, ?)", (seq, text))

    def _unindex(
        self, connection: sqlite3.Connection, account_id: str, profile: str, key: str
    ) -> None:
        """Remove one row's searchable projection, by the docid the row carries."""
        connection.execute(
            "DELETE FROM fields_fts WHERE rowid = ("
            "  SELECT seq FROM fields WHERE account_id = ? AND profile = ? AND key = ?"
            ")",
            (account_id, profile, key),
        )


def _next_seq(connection: sqlite3.Connection, name: str) -> int:
    """Allocate a full-text docid. Monotonic, and never reused. See ADR-0008."""
    row = connection.execute(
        "UPDATE fts_sequence SET next = next + 1 WHERE name = ? RETURNING next", (name,)
    ).fetchone()
    return int(row["next"])


def _prefixed(columns: str, alias: str) -> str:
    """Qualify a column list, for the joins where two tables share a name."""
    return ", ".join(f"{alias}.{column.strip()}" for column in columns.split(","))


def _conditions(
    account_id: str, profile: str, filters: FieldFilters
) -> tuple[list[str], list[Any]]:
    """Turn filters into a WHERE clause, in the order the index expects them."""
    where = ["account_id = ?", "profile = ?"]
    parameters: list[Any] = [account_id, profile]

    if not filters.include_forgotten:
        where.append("forgotten_at IS NULL")
    if filters.source is not None:
        where.append("source = ?")
        parameters.append(filters.source.value)
    if filters.pinned is not None:
        where.append("pinned = ?")
        parameters.append(int(filters.pinned))
    if filters.key_prefix is not None:
        # A range rather than LIKE, so the fields primary key serves it as a seek.
        prefix = normalize_key(filters.key_prefix) if filters.key_prefix.strip() else ""
        where.append("key >= ? AND key < ?")
        parameters.extend([prefix, prefix + "￿"])
    if filters.keys:
        placeholders = ", ".join("?" for _ in filters.keys)
        where.append(f"key IN ({placeholders})")
        parameters.extend(normalize_key(key) for key in filters.keys)
    if filters.since is not None:
        where.append("updated_at >= ?")
        parameters.append(to_column(filters.since))
    if filters.until is not None:
        where.append("updated_at < ?")
        parameters.append(to_column(filters.until))

    return where, parameters


def _page(rows: list[sqlite3.Row], limit: int) -> Page[Field]:
    fields = [_field_of(row) for row in rows[:limit]]
    if len(rows) <= limit:
        return Page(items=fields, next_cursor=None)
    last = fields[-1]
    return Page(items=fields, next_cursor=encode_cursor(to_column(last.updated_at), last.key))


def _field_of(row: sqlite3.Row) -> Field:
    return Field(
        field_id=row["field_id"],
        account_id=row["account_id"],
        profile=row["profile"],
        key=row["key"],
        description=row["description"],
        value=json.loads(row["value_json"]),
        value_type=ValueType(row["value_type"]),
        source=Source(row["source"]),
        asserted_by=row["asserted_by"],
        pinned=bool(row["pinned"]),
        revision=row["revision"],
        created_at=from_column(row["created_at"]),
        updated_at=from_column(row["updated_at"]),
        forgotten_at=from_column_optional(row["forgotten_at"]),
    )
