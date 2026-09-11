"""The identity card as rows, and the cascade that is the only hard delete here."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.errors import LimitExceededError, PersonaExistsError
from persona_api.domain.personas import Persona, new_persona_id
from persona_api.personas.store import PersonaStore
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.fakes.clock import EPOCH

if TYPE_CHECKING:
    from datetime import datetime

    from persona_api.storage.database import Database


def make(
    *,
    account_id: str = ACCOUNT,
    profile: str = PROFILE,
    display_name: str | None = None,
    created_at: datetime = EPOCH,
) -> Persona:
    return Persona(
        persona_id=new_persona_id(),
        account_id=account_id,
        profile=profile,
        display_name=display_name,
        pronouns=None,
        summary=None,
        created_at=created_at,
        updated_at=created_at,
    )


class TestAdding:
    async def test_a_persona_comes_back_as_it_was_written(self, store: PersonaStore) -> None:
        persona = make(display_name="Ada")
        await store.add(persona, cap=20)

        assert await store.get(ACCOUNT, PROFILE) == persona

    async def test_the_same_profile_twice_is_refused(self, store: PersonaStore) -> None:
        await store.add(make(), cap=20)

        with pytest.raises(PersonaExistsError):
            await store.add(make(), cap=20)

    async def test_two_accounts_may_each_have_the_same_profile_name(
        self, store: PersonaStore
    ) -> None:
        # Profiles are scoped to an account, so "work" being taken by somebody else
        # does not affect you.
        await store.add(make(account_id=ACCOUNT), cap=20)
        await store.add(make(account_id=OTHER_ACCOUNT), cap=20)

        assert await store.count_all() == 2

    async def test_one_account_may_have_several_profiles(self, store: PersonaStore) -> None:
        # A work assistant and a home assistant are different people.
        await store.add(make(profile="work"), cap=20)
        await store.add(make(profile="home"), cap=20)

        assert len(await store.list_for_account(ACCOUNT)) == 2


class TestTheCap:
    async def test_it_refuses_the_one_past_the_limit(self, store: PersonaStore) -> None:
        for index in range(3):
            await store.add(make(profile=f"p{index}"), cap=3)

        with pytest.raises(LimitExceededError, match="3 personas"):
            await store.add(make(profile="one-too-many"), cap=3)

    async def test_it_holds_under_concurrent_creates(self, store: PersonaStore) -> None:
        # Counted inside the transaction that writes. A count taken before the write
        # goes stale between the two, and two concurrent creates both pass it.
        await asyncio.gather(
            *(store.add(make(profile=f"p{index:02d}"), cap=5) for index in range(30)),
            return_exceptions=True,
        )

        assert await store.count_for_account(ACCOUNT) == 5

    async def test_one_accounts_personas_do_not_count_against_anothers(
        self, store: PersonaStore
    ) -> None:
        for index in range(3):
            await store.add(make(account_id=OTHER_ACCOUNT, profile=f"p{index}"), cap=3)

        await store.add(make(account_id=ACCOUNT), cap=3)

        assert await store.count_for_account(ACCOUNT) == 1


class TestIsolation:
    async def test_one_account_cannot_read_anothers_persona(self, store: PersonaStore) -> None:
        await store.add(make(account_id=OTHER_ACCOUNT), cap=20)

        assert await store.get(ACCOUNT, PROFILE) is None

    async def test_one_account_cannot_list_anothers_personas(self, store: PersonaStore) -> None:
        await store.add(make(account_id=OTHER_ACCOUNT), cap=20)

        assert await store.list_for_account(ACCOUNT) == []

    async def test_one_account_cannot_delete_anothers_persona(self, store: PersonaStore) -> None:
        await store.add(make(account_id=OTHER_ACCOUNT), cap=20)

        assert await store.delete(ACCOUNT, PROFILE) is False
        assert await store.get(OTHER_ACCOUNT, PROFILE) is not None

    async def test_one_account_cannot_update_anothers_persona(self, store: PersonaStore) -> None:
        await store.add(make(account_id=OTHER_ACCOUNT, display_name="theirs"), cap=20)

        updated = await store.update(
            ACCOUNT, PROFILE, display_name="mine", pronouns=None, summary=None, now=EPOCH
        )

        assert updated is None
        theirs = await store.get(OTHER_ACCOUNT, PROFILE)
        assert theirs is not None
        assert theirs.display_name == "theirs"


class TestUpdating:
    async def test_it_replaces_the_card(self, store: PersonaStore) -> None:
        await store.add(make(display_name="Ada"), cap=20)

        updated = await store.update(
            ACCOUNT,
            PROFILE,
            display_name="Grace",
            pronouns="they/them",
            summary="dry",
            now=EPOCH,
        )

        assert updated is not None
        assert (updated.display_name, updated.pronouns, updated.summary) == (
            "Grace",
            "they/them",
            "dry",
        )

    async def test_a_card_field_can_be_cleared(self, store: PersonaStore) -> None:
        # Which is why the store takes every field including unchanged ones: only the
        # caller knows the difference between "leave this alone" and "clear it".
        await store.add(make(display_name="Ada"), cap=20)

        updated = await store.update(
            ACCOUNT, PROFILE, display_name=None, pronouns=None, summary=None, now=EPOCH
        )

        assert updated is not None
        assert updated.display_name is None

    async def test_updating_something_absent_reports_it(self, store: PersonaStore) -> None:
        assert (
            await store.update(
                ACCOUNT, "never-made", display_name=None, pronouns=None, summary=None, now=EPOCH
            )
            is None
        )


class TestDeleting:
    async def test_it_reports_whether_there_was_one(self, store: PersonaStore) -> None:
        await store.add(make(), cap=20)

        assert await store.delete(ACCOUNT, PROFILE) is True
        assert await store.delete(ACCOUNT, PROFILE) is False

    async def test_it_takes_the_fields_and_notes_with_it(
        self, store: PersonaStore, database: Database
    ) -> None:
        await store.add(make(), cap=20)
        await database.execute(
            "INSERT INTO fields (account_id, profile, key, field_id, seq, description,"
            " value_json, value_type, source, asserted_by, created_at, updated_at)"
            " VALUES (?, ?, 'voice', 'fld_1', 1, 'd', '\"x\"', 'string', 'assistant',"
            " 'persona', '2026-01-01', '2026-01-01')",
            (ACCOUNT, PROFILE),
        )

        await store.delete(ACCOUNT, PROFILE)

        assert await database.count("SELECT count(*) AS total FROM fields") == 0

    async def test_it_does_not_take_the_record_that_it_happened(
        self, store: PersonaStore, database: Database
    ) -> None:
        # The event log has no foreign key to personas precisely so that deleting one
        # cannot delete the record that it was deleted.
        await store.add(make(), cap=20)
        await database.execute(
            "INSERT INTO events (event_id, at, account_id, profile, action, subject,"
            " detail, source, asserted_by)"
            " VALUES ('evt_1', '2026-01-01', ?, ?, 'persona.created', ?, '', 'assistant',"
            " 'persona')",
            (ACCOUNT, PROFILE, PROFILE),
        )

        await store.delete(ACCOUNT, PROFILE)

        assert await database.count("SELECT count(*) AS total FROM events") == 1
