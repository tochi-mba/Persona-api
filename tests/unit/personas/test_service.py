"""The identity block, the export, recall -- and the rule that reads never create."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.domain.errors import (
    CredentialRefusedError,
    InvalidProfileError,
    PersonaExistsError,
    PersonaNotFoundError,
)
from persona_api.domain.notes import NoteKind
from persona_api.domain.provenance import Source
from persona_api.events.log import EventAction
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.unit.personas.conftest import ASSERTED_BY

if TYPE_CHECKING:
    from persona_api.events.sql_log import SqlEventLog
    from persona_api.memory.fields import FieldStore
    from persona_api.memory.notes import NoteStore
    from persona_api.personas.service import PersonaService


async def make(service: PersonaService, *, profile: str = PROFILE, **card: str) -> None:
    await service.create(
        account_id=ACCOUNT,
        profile=profile,
        source=Source.ASSISTANT,
        asserted_by=ASSERTED_BY,
        **card,
    )


class TestCreating:
    async def test_it_normalizes_the_profile(self, service: PersonaService) -> None:
        persona = await service.create(
            account_id=ACCOUNT,
            profile="  Work  ",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert persona.profile == "work"

    async def test_a_profile_that_cannot_address_a_persona_is_refused(
        self, service: PersonaService
    ) -> None:
        with pytest.raises(InvalidProfileError):
            await service.create(
                account_id=ACCOUNT,
                profile="not a profile!",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_a_typo_is_a_new_empty_persona_rather_than_an_error(
        self, service: PersonaService
    ) -> None:
        # persona-api cannot check that a profile exists in keyring -- there is no
        # endpoint that would answer. list_personas is how anybody notices, which is why
        # this is acceptable rather than merely unavoidable.
        await make(service, profile="work")
        await make(service, profile="wrok")

        assert [p.profile for p in await service.list_for_account(ACCOUNT)] == ["work", "wrok"]

    async def test_blank_card_text_is_stored_as_absent(self, service: PersonaService) -> None:
        persona = await service.create(
            account_id=ACCOUNT,
            profile=PROFILE,
            display_name="   ",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        assert persona.display_name is None

    async def test_a_credential_in_a_card_field_is_refused(self, service: PersonaService) -> None:
        # A display name is an odd place to put an API key, which is exactly why
        # somebody will.
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await service.create(
                account_id=ACCOUNT,
                profile=PROFILE,
                summary="my key is " + "AKIA" + "IOSFODNN7EXAMPLE",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_creating_twice_is_refused(self, service: PersonaService) -> None:
        await make(service)

        with pytest.raises(PersonaExistsError):
            await make(service)

    async def test_it_records_an_event(self, service: PersonaService, events: SqlEventLog) -> None:
        await make(service)

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert [event.action for event in recorded] == [EventAction.PERSONA_CREATED]


class TestEnsure:
    async def test_it_creates_on_the_first_write(self, service: PersonaService) -> None:
        # So an assistant can record something without first calling create_persona.
        persona = await service.ensure(
            account_id=ACCOUNT, profile=PROFILE, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert persona.profile == PROFILE

    async def test_it_returns_the_existing_one_afterwards(self, service: PersonaService) -> None:
        first = await service.ensure(
            account_id=ACCOUNT, profile=PROFILE, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )
        second = await service.ensure(
            account_id=ACCOUNT, profile=PROFILE, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert first.persona_id == second.persona_id

    async def test_reading_never_creates(self, service: PersonaService) -> None:
        # A GET that created a row would make "does this persona exist" unanswerable,
        # and would turn a typo'd profile into a persona nobody asked for at the moment
        # somebody was only looking.
        with pytest.raises(PersonaNotFoundError):
            await service.get(ACCOUNT, "never-made")

        assert await service.list_for_account(ACCOUNT) == []


class TestIsolation:
    async def test_one_account_gets_the_same_not_found_as_for_one_that_never_existed(
        self, service: PersonaService
    ) -> None:
        await service.create(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        with pytest.raises(PersonaNotFoundError) as theirs:
            await service.get(ACCOUNT, PROFILE)
        with pytest.raises(PersonaNotFoundError) as nobodys:
            await service.get(ACCOUNT, "never-made")

        # Byte-identical. A different message would confirm that somebody else has a
        # persona for this profile.
        assert str(theirs.value) == str(nobodys.value)

    async def test_one_account_cannot_update_anothers_persona(
        self, service: PersonaService
    ) -> None:
        await service.create(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        with pytest.raises(PersonaNotFoundError):
            await service.update(
                account_id=ACCOUNT,
                profile=PROFILE,
                display_name="mine now",
                pronouns=None,
                summary=None,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

    async def test_one_account_cannot_delete_anothers_persona(
        self, service: PersonaService
    ) -> None:
        await service.create(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        with pytest.raises(PersonaNotFoundError):
            await service.delete(
                account_id=ACCOUNT,
                profile=PROFILE,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )


class TestUpdating:
    async def test_it_replaces_the_card_and_records_it(
        self, service: PersonaService, events: SqlEventLog
    ) -> None:
        await make(service, display_name="Ada")

        updated = await service.update(
            account_id=ACCOUNT,
            profile=PROFILE,
            display_name="Grace",
            pronouns="they/them",
            summary=None,
            source=Source.OWNER,
            asserted_by=ASSERTED_BY,
        )

        assert updated.display_name == "Grace"
        recorded, _ = await events.recent(ACCOUNT, PROFILE)
        assert recorded[0].action is EventAction.PERSONA_UPDATED

    async def test_a_credential_in_an_update_is_refused(self, service: PersonaService) -> None:
        await make(service)

        with pytest.raises(CredentialRefusedError):
            await service.update(
                account_id=ACCOUNT,
                profile=PROFILE,
                display_name=None,
                pronouns=None,
                summary="AKIA" + "IOSFODNN7EXAMPLE",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )


class TestDeleting:
    async def test_it_records_that_it_happened(
        self, service: PersonaService, events: SqlEventLog
    ) -> None:
        await make(service)

        await service.delete(
            account_id=ACCOUNT, profile=PROFILE, source=Source.OWNER, asserted_by=ASSERTED_BY
        )

        recorded, _ = await events.recent(ACCOUNT, PROFILE)
        assert recorded[0].action is EventAction.PERSONA_DELETED

    async def test_deleting_one_that_is_not_there_is_refused(self, service: PersonaService) -> None:
        with pytest.raises(PersonaNotFoundError):
            await service.delete(
                account_id=ACCOUNT,
                profile="never-made",
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )


class TestTheIdentityBlock:
    async def test_it_carries_only_the_pinned_entries(
        self, service: PersonaService, fields: FieldStore, notes: NoteStore
    ) -> None:
        # Pinned rather than everything, because this is what goes into a prompt at the
        # start of a turn. The caps are a token budget, not a preference.
        await make(service, display_name="Ada")
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="d",
            value="dry",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            pinned=True,
        )
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="pace",
            description="d",
            value="slow",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )
        await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="pinned note",
            kind=NoteKind.LESSON,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
            pinned=True,
        )
        await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="ordinary note",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        identity = await service.identity(ACCOUNT, PROFILE)

        assert [field.key for field in identity.fields] == ["voice"]
        assert [note.body for note in identity.notes] == ["pinned note"]

    async def test_it_reports_counts_so_an_assistant_knows_there_is_more(
        self, service: PersonaService, fields: FieldStore, notes: NoteStore
    ) -> None:
        await make(service)
        for index in range(4):
            await fields.set(
                account_id=ACCOUNT,
                profile=PROFILE,
                key=f"key{index}",
                description="d",
                value=index,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )
        await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="one note",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        identity = await service.identity(ACCOUNT, PROFILE)

        assert identity.field_count == 4
        assert identity.note_count == 1
        assert identity.fields == []

    async def test_it_is_not_found_for_a_persona_that_is_not_there(
        self, service: PersonaService
    ) -> None:
        with pytest.raises(PersonaNotFoundError):
            await service.identity(ACCOUNT, "never-made")

    async def test_there_is_no_rendered_prose_anywhere_on_it(self, service: PersonaService) -> None:
        # Turning a persona into a system-prompt string is the MCP layer's job. An
        # endpoint that returned one would be an endpoint that gets pasted into one --
        # see docs/adr/0001-data-not-instructions.md.
        await make(service, display_name="Ada")

        identity = await service.identity(ACCOUNT, PROFILE)

        assert not hasattr(identity, "prompt")
        assert not hasattr(identity, "rendered")
        assert not hasattr(identity, "text")


class TestExport:
    async def test_it_returns_everything_in_one_call(
        self, service: PersonaService, fields: FieldStore, notes: NoteStore
    ) -> None:
        await make(service)
        for index in range(3):
            await fields.set(
                account_id=ACCOUNT,
                profile=PROFILE,
                key=f"key{index}",
                description="d",
                value=index,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )
            await notes.write(
                account_id=ACCOUNT,
                profile=PROFILE,
                body=f"note {index}",
                kind=NoteKind.EPISODE,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

        export = await service.export(ACCOUNT, PROFILE, limit=20)

        assert len(export.fields.items) == 3
        assert len(export.notes.items) == 3

    async def test_the_two_halves_page_independently(
        self, service: PersonaService, fields: FieldStore, notes: NoteStore
    ) -> None:
        # A persona can be long in fields and short in notes, so one cursor for both
        # would stop the walk at whichever ran out first.
        await make(service)
        for index in range(5):
            await fields.set(
                account_id=ACCOUNT,
                profile=PROFILE,
                key=f"key{index}",
                description="d",
                value=index,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )
        await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="the only note",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        export = await service.export(ACCOUNT, PROFILE, limit=2)

        assert export.fields.next_cursor is not None
        assert export.notes.next_cursor is None

    async def test_forgotten_rows_are_out_by_default_and_in_when_asked(
        self, service: PersonaService, notes: NoteStore
    ) -> None:
        await make(service)
        note = await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="dropped",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )
        await notes.forget(
            ACCOUNT, PROFILE, note.note_id, source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        default = await service.export(ACCOUNT, PROFILE, limit=20)
        everything = await service.export(ACCOUNT, PROFILE, limit=20, include_forgotten=True)

        assert default.notes.items == []
        assert len(everything.notes.items) == 1

    async def test_it_is_not_found_for_another_accounts_persona(
        self, service: PersonaService
    ) -> None:
        await service.create(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        with pytest.raises(PersonaNotFoundError):
            await service.export(ACCOUNT, PROFILE, limit=20)


class TestRecall:
    async def test_it_returns_two_sections_rather_than_one_merged_ranking(
        self, service: PersonaService, fields: FieldStore, notes: NoteStore
    ) -> None:
        # Merging them would compare bm25 scores computed over two different corpora,
        # which is not a meaningful comparison -- it would look tidier and quietly
        # mis-rank. Two sections also tells a model which kind of thing it is reading.
        await make(service)
        await fields.set(
            account_id=ACCOUNT,
            profile=PROFILE,
            key="voice",
            description="how it speaks",
            value="dry and concise",
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )
        await notes.write(
            account_id=ACCOUNT,
            profile=PROFILE,
            body="they asked for concise answers",
            kind=NoteKind.LESSON,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        recall = await service.recall(ACCOUNT, query="concise", limit=20)

        assert [field.key for field in recall.fields] == ["voice"]
        assert [note.kind for note in recall.notes] == [NoteKind.LESSON]

    async def test_it_can_span_every_persona_the_account_owns(
        self, service: PersonaService, notes: NoteStore
    ) -> None:
        # recall_everywhere: each hit says which persona it came from.
        await make(service, profile="work")
        await make(service, profile="home")
        for profile in ("work", "home"):
            await notes.write(
                account_id=ACCOUNT,
                profile=profile,
                body=f"a rewrite at {profile}",
                kind=NoteKind.EPISODE,
                source=Source.ASSISTANT,
                asserted_by=ASSERTED_BY,
            )

        everywhere = await service.recall(ACCOUNT, query="rewrite", limit=20)
        scoped = await service.recall(ACCOUNT, query="rewrite", profile="work", limit=20)

        assert sorted(note.profile for note in everywhere.notes) == ["home", "work"]
        assert [note.profile for note in scoped.notes] == ["work"]

    async def test_searching_across_personas_never_reaches_another_account(
        self, service: PersonaService, notes: NoteStore
    ) -> None:
        await service.create(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )
        await notes.write(
            account_id=OTHER_ACCOUNT,
            profile=PROFILE,
            body="a rewrite of theirs",
            kind=NoteKind.EPISODE,
            source=Source.ASSISTANT,
            asserted_by=ASSERTED_BY,
        )

        recall = await service.recall(ACCOUNT, query="rewrite", limit=20)

        assert recall.notes == []
