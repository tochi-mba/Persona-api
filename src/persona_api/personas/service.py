"""What a persona *is*, assembled from the three stores that hold its parts.

The store holds the card. This holds the rules about the card, and the three reads that
are the reason an assistant calls this service at all.

## The identity block

:meth:`PersonaService.identity` is the main read: the card, the **pinned** fields and
notes, and counts. Pinned rather than everything, because this is what goes into a prompt
at the start of a turn -- so the pin caps are a token budget rather than a preference, and
the counts are there so an assistant can tell that there is more to ask for.

**There is deliberately no endpoint that returns rendered prose.** Turning this into a
system-prompt string is the MCP layer's job. Baking a prompt template in here would
freeze it, put presentation in the wrong service, and -- the part that matters -- produce
an endpoint that returns a system prompt, which is an endpoint that gets pasted into one.
See ``docs/adr/0001-data-not-instructions.md``.

## Recall returns two lists, never one merged ranking

``fields`` and ``notes`` are each ranked by ``bm25`` **within their own index**. Merging
them would mean comparing scores computed over two different corpora, which is not a
meaningful comparison: it would look tidier and quietly mis-rank. Two sections is also
more useful to a model, because structured attributes and narrative notes are different
things and it should know which it is reading.

## Getting a persona is not creating one

:meth:`PersonaService.ensure` creates on first write, so an assistant does not have to
call ``create_persona`` before it can record anything -- but reads never create. A ``GET``
that created a row would make "does this persona exist" unanswerable, and would turn a
typo'd profile into a persona nobody asked for at the moment somebody was only looking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_api.domain.errors import PersonaNotFoundError
from persona_api.domain.personas import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_PRONOUNS_LENGTH,
    MAX_SUMMARY_LENGTH,
    Persona,
    new_persona_id,
    normalize_card_text,
    normalize_profile,
)
from persona_api.domain.secrets import refuse_if_credential
from persona_api.events.log import EventAction
from persona_api.memory.filters import FieldFilters, NoteFilters

if TYPE_CHECKING:
    from persona_api.core.clock import Clock
    from persona_api.core.config import Settings
    from persona_api.domain.fields import Field
    from persona_api.domain.notes import Note
    from persona_api.domain.provenance import Source
    from persona_api.events.log import EventLog
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.memory.pagination import Page
    from persona_api.personas.store import PersonaStore


@dataclass(frozen=True, slots=True)
class Identity:
    """The card, what is pinned into the prompt, and how much more there is."""

    persona: Persona
    fields: list[Field]
    notes: list[Note]
    field_count: int
    note_count: int
    """Live counts, so an assistant can tell there is more than what is pinned."""


@dataclass(frozen=True, slots=True)
class Recall:
    """Search results, kept in two sections. See the module docstring."""

    fields: list[Field]
    notes: list[Note]


@dataclass(frozen=True, slots=True)
class Export:
    """A whole persona in one response, paginated by the same cursors as every list."""

    persona: Persona
    fields: Page[Field]
    notes: Page[Note]


class PersonaService:
    """The identity card, and the reads that assemble a persona from its parts."""

    # PLR0913: six collaborators -- three stores, the log, the clock and settings. This
    # is the composition point, so it is the one place that legitimately holds them all.
    def __init__(  # noqa: PLR0913
        self,
        *,
        personas: PersonaStore,
        fields: FieldStore,
        notes: NoteStore,
        events: EventLog,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._personas = personas
        self._fields = fields
        self._notes = notes
        self._events = events
        self._clock = clock
        self._settings = settings

    # PLR0913: the card's three fields plus the provenance of whoever created it.
    async def create(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        profile: str,
        display_name: str | None = None,
        pronouns: str | None = None,
        summary: str | None = None,
        source: Source,
        asserted_by: str,
    ) -> Persona:
        """Create a persona for one profile.

        Raises:
            InvalidProfileError: the profile name cannot be stored or addressed.
            PersonaExistsError: this account already has one for that profile.
            LimitExceededError: the account is at its persona cap.
            CredentialRefusedError: a card field looks like a credential.
        """
        normalized = normalize_profile(profile)
        card = self._card(display_name, pronouns, summary)
        now = self._clock.now()

        persona = Persona(
            persona_id=new_persona_id(),
            account_id=account_id,
            profile=normalized,
            display_name=card[0],
            pronouns=card[1],
            summary=card[2],
            created_at=now,
            updated_at=now,
        )
        await self._personas.add(persona, cap=self._settings.max_personas_per_account)
        await self._events.record(
            action=EventAction.PERSONA_CREATED,
            account_id=account_id,
            profile=normalized,
            subject=normalized,
            source=source,
            asserted_by=asserted_by,
        )
        return persona

    async def ensure(
        self, *, account_id: str, profile: str, source: Source, asserted_by: str
    ) -> Persona:
        """Return this account's persona for a profile, creating it if there is none.

        The write path calls this so an assistant can record something without first
        calling ``create_persona``. Reads deliberately do not -- see the module
        docstring.
        """
        normalized = normalize_profile(profile)
        existing = await self._personas.get(account_id, normalized)
        if existing is not None:
            return existing
        return await self.create(
            account_id=account_id,
            profile=normalized,
            source=source,
            asserted_by=asserted_by,
        )

    async def get(self, account_id: str, profile: str) -> Persona:
        """One persona.

        Raises:
            PersonaNotFoundError: no such persona for this account. Identical to the
                answer for another account's persona, so the API never confirms one
                exists.
        """
        persona = await self._personas.get(account_id, normalize_profile(profile))
        if persona is None:
            raise PersonaNotFoundError(_MISSING)
        return persona

    async def list_for_account(self, account_id: str) -> list[Persona]:
        """Every persona this account owns."""
        return await self._personas.list_for_account(account_id)

    # PLR0913: the card's three fields, plus who is changing them.
    async def update(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        profile: str,
        display_name: str | None,
        pronouns: str | None,
        summary: str | None,
        source: Source,
        asserted_by: str,
    ) -> Persona:
        """Replace the card. Every field is passed, including unchanged ones.

        Merging is the caller's job because only the caller knows the difference between
        "leave this alone" and "clear it" -- a distinction the router makes explicit by
        reading which keys the request body actually contained.

        Raises:
            PersonaNotFoundError: no such persona for this account.
            CredentialRefusedError: a card field looks like a credential.
        """
        normalized = normalize_profile(profile)
        card = self._card(display_name, pronouns, summary)

        updated = await self._personas.update(
            account_id,
            normalized,
            display_name=card[0],
            pronouns=card[1],
            summary=card[2],
            now=self._clock.now(),
        )
        if updated is None:
            raise PersonaNotFoundError(_MISSING)

        await self._events.record(
            action=EventAction.PERSONA_UPDATED,
            account_id=account_id,
            profile=normalized,
            subject=normalized,
            source=source,
            asserted_by=asserted_by,
        )
        return updated

    async def delete(
        self, *, account_id: str, profile: str, source: Source, asserted_by: str
    ) -> None:
        """Delete a persona and everything it holds. The only hard delete here.

        Raises:
            PersonaNotFoundError: no such persona for this account.
        """
        normalized = normalize_profile(profile)
        if not await self._personas.delete(account_id, normalized):
            raise PersonaNotFoundError(_MISSING)

        # Recorded after the delete rather than inside it, and this is the one place
        # that is right: the event has no foreign key to the persona precisely so it
        # can outlive it, and a cascade that took the record of its own deletion with
        # it would leave nothing to say the persona had ever existed.
        await self._events.record(
            action=EventAction.PERSONA_DELETED,
            account_id=account_id,
            profile=normalized,
            subject=normalized,
            source=source,
            asserted_by=asserted_by,
        )

    async def identity(self, account_id: str, profile: str) -> Identity:
        """The card, the pinned entries, and the counts. The main read.

        Raises:
            PersonaNotFoundError: no such persona for this account.
        """
        persona = await self.get(account_id, profile)
        return Identity(
            persona=persona,
            fields=await self._fields.pinned(account_id, persona.profile),
            notes=await self._notes.pinned(account_id, persona.profile),
            field_count=await self._fields.count(account_id, persona.profile),
            note_count=await self._notes.count(account_id, persona.profile),
        )

    # PLR0913: a persona plus two independent cursors, because the two halves of an
    # export are paged separately -- a persona can be long in fields and short in notes.
    async def export(  # noqa: PLR0913
        self,
        account_id: str,
        profile: str,
        *,
        limit: int,
        field_cursor: str | None = None,
        note_cursor: str | None = None,
        include_forgotten: bool = False,
    ) -> Export:
        """Everything, in one response, so an assistant makes one call rather than thirty.

        Raises:
            PersonaNotFoundError: no such persona for this account.
        """
        persona = await self.get(account_id, profile)
        return Export(
            persona=persona,
            fields=await self._fields.list_for_persona(
                account_id,
                persona.profile,
                filters=FieldFilters(include_forgotten=include_forgotten),
                limit=limit,
                cursor=field_cursor,
            ),
            notes=await self._notes.list_for_persona(
                account_id,
                persona.profile,
                filters=NoteFilters(include_forgotten=include_forgotten),
                limit=limit,
                cursor=note_cursor,
            ),
        )

    async def recall(
        self, account_id: str, *, query: str, profile: str | None = None, limit: int
    ) -> Recall:
        """Search one persona, or every persona this account owns.

        ``profile=None`` searches across all of them, and each hit says which persona it
        came from -- which is what ``recall_everywhere`` is for.

        Raises:
            InvalidSearchError: the query holds no word to search for.
        """
        scope = None if profile is None else normalize_profile(profile)
        return Recall(
            fields=await self._fields.search(account_id, query=query, profile=scope, limit=limit),
            notes=await self._notes.search(account_id, query=query, profile=scope, limit=limit),
        )

    def _card(
        self, display_name: str | None, pronouns: str | None, summary: str | None
    ) -> tuple[str | None, str | None, str | None]:
        """Normalize and vet the three card fields.

        The credential check covers these too. A display name is an odd place to put an
        API key, which is exactly why somebody will.
        """
        normalized = (
            _text(display_name, what="display name", limit=MAX_DISPLAY_NAME_LENGTH),
            _text(pronouns, what="pronouns", limit=MAX_PRONOUNS_LENGTH),
            _text(summary, what="summary", limit=MAX_SUMMARY_LENGTH),
        )
        for value in normalized:
            if value is not None:
                refuse_if_credential(value)
        return normalized


_MISSING = "no persona for that profile"
"""One message, whether the persona is absent or belongs to somebody else."""


def _text(raw: str | None, *, what: str, limit: int) -> str | None:
    return None if raw is None else normalize_card_text(raw, what=what, limit=limit)
