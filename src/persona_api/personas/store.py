"""Personas as rows, keyed by the pair that defines one.

``(account_id, profile)`` is the primary key rather than something checked after a read,
which is what makes account isolation structural here: there is no call that *could*
reach another account's persona, so it is not a rule somebody has to remember in a
handler.

The card itself is deliberately tiny -- a name, pronouns, a sentence. Everything else an
assistant might want to remember about itself is a field or a note, because those grow
without a migration and this does not. A summary is read on every turn, so its size is a
prompt cost rather than a storage one.

**Delete is the only hard delete in the service**, and it cascades: fields and notes go
through the foreign key, and their full-text rows go through the triggers the migration
installs. The event log does not go, because deleting a persona must not delete the
record that it was deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.domain.errors import LimitExceededError, PersonaExistsError
from persona_api.domain.personas import Persona
from persona_api.storage.times import from_column, to_column

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from persona_api.storage.database import Database

COLUMNS = "account_id, profile, persona_id, display_name, pronouns, summary, created_at, updated_at"


class PersonaStore:
    """The identity cards, one per ``(account_id, profile)``."""

    def __init__(self, *, database: Database) -> None:
        self._db = database

    async def add(self, persona: Persona, *, cap: int) -> None:
        """Store a new persona, refusing to take the account past ``cap``.

        Raises:
            PersonaExistsError: this account already has a persona for that profile.
            LimitExceededError: the account is already at ``cap``.
        """

        def write(connection: sqlite3.Connection) -> None:
            taken = connection.execute(
                "SELECT 1 FROM personas WHERE account_id = ? AND profile = ?",
                (persona.account_id, persona.profile),
            ).fetchone()
            if taken is not None:
                msg = f"a persona for the profile {persona.profile!r} already exists"
                raise PersonaExistsError(msg)

            # Counted inside the transaction that writes, so two concurrent creates
            # cannot both pass a count taken before either of them wrote.
            held = connection.execute(
                "SELECT count(*) AS total FROM personas WHERE account_id = ?",
                (persona.account_id,),
            ).fetchone()["total"]
            if held >= cap:
                msg = f"at most {cap} personas per account"
                raise LimitExceededError(msg)

            connection.execute(
                f"INSERT INTO personas ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
                (
                    persona.account_id,
                    persona.profile,
                    persona.persona_id,
                    persona.display_name,
                    persona.pronouns,
                    persona.summary,
                    to_column(persona.created_at),
                    to_column(persona.updated_at),
                ),
            )

        await self._db.transact(write)

    async def get(self, account_id: str, profile: str) -> Persona | None:
        """One persona, or ``None``.

        ``None`` covers both "no such profile" and "somebody else's persona", because a
        distinguishable answer would tell one person that another has a persona.
        """
        row = await self._db.fetch_one(
            f"SELECT {COLUMNS} FROM personas WHERE account_id = ? AND profile = ?",  # noqa: S608
            (account_id, profile),
        )
        return None if row is None else _persona_of(row)

    async def list_for_account(self, account_id: str) -> list[Persona]:
        """Every persona this account owns, oldest first.

        The endpoint that makes a typo'd profile visible rather than silent -- see
        ``docs/adr/0005-one-persona-per-profile.md``.
        """
        rows = await self._db.fetch_all(
            f"SELECT {COLUMNS} FROM personas WHERE account_id = ? "  # noqa: S608
            "ORDER BY created_at, profile",
            (account_id,),
        )
        return [_persona_of(row) for row in rows]

    # PLR0913: three independently optional card fields, plus who and when.
    async def update(  # noqa: PLR0913
        self,
        account_id: str,
        profile: str,
        *,
        display_name: str | None,
        pronouns: str | None,
        summary: str | None,
        now: datetime,
    ) -> Persona | None:
        """Replace the card fields that are named. Returns ``None`` if there is no persona.

        Every field is passed, including the ones that are staying as they were: the
        caller has already merged, because only it knows the difference between "leave
        this alone" and "clear it". A store that guessed would make clearing a pronoun
        impossible.
        """

        def write(connection: sqlite3.Connection) -> Persona | None:
            touched = connection.execute(
                "UPDATE personas SET display_name = ?, pronouns = ?, summary = ?, updated_at = ?"
                " WHERE account_id = ? AND profile = ?",
                (display_name, pronouns, summary, to_column(now), account_id, profile),
            ).rowcount
            if not touched:
                return None
            return _persona_of(
                connection.execute(
                    f"SELECT {COLUMNS} FROM personas WHERE account_id = ? AND profile = ?",  # noqa: S608
                    (account_id, profile),
                ).fetchone()
            )

        return await self._db.transact(write)

    async def delete(self, account_id: str, profile: str) -> bool:
        """Remove a persona and everything it owns. Returns whether there was one.

        The fields and notes go through the foreign key, and their full-text rows go
        through the triggers -- not through a hand-ordered sequence of deletes with a
        documented lesser harm if it failed partway.
        """
        removed = await self._db.execute(
            "DELETE FROM personas WHERE account_id = ? AND profile = ?", (account_id, profile)
        )
        return removed > 0

    async def count_for_account(self, account_id: str) -> int:
        """How many personas this account owns, for the per-account cap."""
        return await self._db.count(
            "SELECT count(*) AS total FROM personas WHERE account_id = ?", (account_id,)
        )

    async def count_all(self) -> int:
        """How many personas exist, across every account.

        The one method here that is not account-scoped, and it exists for exactly one
        caller: ``/healthy``, which reports a count and never names an account or a
        profile. Anything else reaching for this is a cross-account read in disguise.
        """
        return await self._db.count("SELECT count(*) AS total FROM personas")


def _persona_of(row: sqlite3.Row) -> Persona:
    return Persona(
        persona_id=row["persona_id"],
        account_id=row["account_id"],
        profile=row["profile"],
        display_name=row["display_name"],
        pronouns=row["pronouns"],
        summary=row["summary"],
        created_at=from_column(row["created_at"]),
        updated_at=from_column(row["updated_at"]),
    )
