"""The field store: idempotent writes, caps that hold, and an index that agrees."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from persona_api.domain.errors import (
    CredentialRefusedError,
    FieldNotFoundError,
    InvalidFieldValueError,
    LimitExceededError,
)
from persona_api.domain.fields import ValueLimits, ValueType
from persona_api.domain.provenance import Source
from persona_api.events.log import EventAction
from persona_api.memory.fields import FieldStore
from persona_api.memory.filters import FieldFilters
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.unit.memory.conftest import ASSERTED_BY, set_field

if TYPE_CHECKING:
    from persona_api.events.sql_log import SqlEventLog
    from persona_api.storage.database import Database
    from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.usefixtures("personas")


class TestSetting:
    async def test_a_new_key_is_created_at_revision_one(self, fields: FieldStore) -> None:
        field = await set_field(fields)

        assert field.key == "voice"
        assert field.revision == 1
        assert field.value == "dry and concise"

    async def test_the_key_is_normalized_on_the_way_in(self, fields: FieldStore) -> None:
        # "Favourite Topics" and "favourite-topics" must reach one row, which is the
        # anti-sprawl mechanism doing its job at the store rather than only in a pure
        # function nobody called.
        await set_field(fields, key="Favourite Topics", value=["jazz"])
        await set_field(fields, key="favourite-topics", value=["jazz", "ambient"])

        assert await fields.count(ACCOUNT, PROFILE) == 1
        stored = await fields.get(ACCOUNT, PROFILE, "FAVOURITE_TOPICS")
        assert stored is not None
        assert stored.value == ["jazz", "ambient"]

    async def test_the_value_type_is_derived_rather_than_taken(self, fields: FieldStore) -> None:
        field = await set_field(fields, key="verbose", value=False)

        # A boolean, not a number. isinstance(True, int) is True in Python, so this is
        # the assertion that would catch the derivation being reordered.
        assert field.value_type is ValueType.BOOLEAN

    async def test_writing_the_same_value_twice_does_not_bump_the_revision(
        self, fields: FieldStore
    ) -> None:
        # A model retrying a call must not make `revision` a count of how often PUT was
        # called. The canonical JSON serialization is what makes the comparison work.
        first = await set_field(fields)
        second = await set_field(fields)

        assert second.revision == first.revision == 1
        assert second.updated_at == first.updated_at

    async def test_an_object_written_with_its_keys_in_another_order_is_unchanged(
        self, fields: FieldStore
    ) -> None:
        await set_field(fields, key="home", value={"city": "Lisbon", "since": 2019})
        second = await set_field(fields, key="home", value={"since": 2019, "city": "Lisbon"})

        assert second.revision == 1

    async def test_a_changed_value_bumps_the_revision_and_the_timestamp(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        await set_field(fields)
        clock.advance(timedelta(hours=1))

        revised = await set_field(fields, value="warmer now")

        assert revised.revision == 2
        assert revised.updated_at > revised.created_at

    async def test_a_changed_description_alone_is_a_revision(self, fields: FieldStore) -> None:
        await set_field(fields)

        revised = await set_field(fields, description="how it speaks, and when")

        assert revised.revision == 2

    async def test_the_created_at_survives_a_revision(
        self, fields: FieldStore, clock: FakeClock
    ) -> None:
        # "Known since" must not jump every time a value is corrected.
        created = await set_field(fields)
        clock.advance(timedelta(days=7))

        revised = await set_field(fields, value="different")

        assert revised.created_at == created.created_at

    async def test_it_is_idempotent_on_the_key_so_a_retry_makes_one_field(
        self, fields: FieldStore
    ) -> None:
        await asyncio.gather(*(set_field(fields) for _ in range(10)))

        assert await fields.count(ACCOUNT, PROFILE) == 1


class TestProvenance:
    async def test_asserted_by_is_recorded_as_given_by_the_verified_token(
        self, fields: FieldStore
    ) -> None:
        field = await set_field(fields)

        assert field.asserted_by == ASSERTED_BY

    async def test_source_round_trips_as_the_claim_it_is(self, fields: FieldStore) -> None:
        field = await set_field(fields, source=Source.OWNER)

        assert field.source is Source.OWNER


class TestRefusingCredentials:
    async def test_a_credential_shaped_value_is_refused(self, fields: FieldStore) -> None:
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await set_field(fields, key="api_key", value="AKIA" + "IOSFODNN7EXAMPLE")

    async def test_a_credential_in_the_description_is_refused_too(self, fields: FieldStore) -> None:
        # A key pasted into a description is as disclosed as one pasted into a value.
        with pytest.raises(CredentialRefusedError):
            await set_field(
                fields, description="the key is " + "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
            )

    async def test_a_credential_inside_a_list_is_refused(self, fields: FieldStore) -> None:
        with pytest.raises(CredentialRefusedError):
            await set_field(fields, key="tokens", value=["fine", "AKIA" + "IOSFODNN7EXAMPLE"])

    async def test_nothing_is_written_when_a_value_is_refused(self, fields: FieldStore) -> None:
        with pytest.raises(CredentialRefusedError):
            await set_field(fields, key="api_key", value="AKIA" + "IOSFODNN7EXAMPLE")

        assert await fields.count(ACCOUNT, PROFILE) == 0


class TestReading:
    async def test_an_unknown_key_reads_as_absent(self, fields: FieldStore) -> None:
        assert await fields.get(ACCOUNT, PROFILE, "never_set") is None

    async def test_a_forgotten_field_is_absent_by_default(self, fields: FieldStore) -> None:
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert await fields.get(ACCOUNT, PROFILE, "voice") is None

    async def test_a_forgotten_field_comes_back_when_asked_for(self, fields: FieldStore) -> None:
        # Soft forget, so an assistant deciding on its own that a memory is stale
        # cannot destroy it.
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        found = await fields.get(ACCOUNT, PROFILE, "voice", include_forgotten=True)

        assert found is not None
        assert found.forgotten_at is not None

    async def test_setting_a_forgotten_key_again_revives_it(self, fields: FieldStore) -> None:
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        revived = await set_field(fields, value="remembered again")

        assert revived.forgotten_at is None
        assert revived.revision == 2

    async def test_forgetting_something_that_is_not_there_is_refused(
        self, fields: FieldStore
    ) -> None:
        with pytest.raises(FieldNotFoundError):
            await fields.forget(
                ACCOUNT, PROFILE, "never_set", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )

    async def test_forgetting_twice_is_refused_the_second_time(self, fields: FieldStore) -> None:
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        with pytest.raises(FieldNotFoundError):
            await fields.forget(
                ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )


class TestTheSchemaEndpoint:
    async def test_it_returns_keys_and_descriptions_and_no_values(self, fields: FieldStore) -> None:
        # The anti-sprawl endpoint, and it has to be cheap enough to call BEFORE
        # inventing a key -- which it is not if it carries every value in the persona.
        await set_field(fields, key="voice", value="dry")
        await set_field(fields, key="tone", description="warmth", value="warm")

        summary = await fields.schema(ACCOUNT, PROFILE)

        assert [entry.key for entry in summary] == ["tone", "voice"]
        assert [entry.description for entry in summary] == ["warmth", "how it speaks"]
        assert not any(hasattr(entry, "value") for entry in summary)

    async def test_it_omits_forgotten_keys(self, fields: FieldStore) -> None:
        await set_field(fields, key="voice")
        await set_field(fields, key="tone")
        await fields.forget(
            ACCOUNT, PROFILE, "tone", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        assert [entry.key for entry in await fields.schema(ACCOUNT, PROFILE)] == ["voice"]


class TestIsolation:
    async def test_one_account_cannot_read_anothers_field(self, fields: FieldStore) -> None:
        await set_field(fields, account_id=OTHER_ACCOUNT)

        assert await fields.get(ACCOUNT, PROFILE, "voice") is None

    async def test_one_account_cannot_forget_anothers_field(self, fields: FieldStore) -> None:
        await set_field(fields, account_id=OTHER_ACCOUNT)

        # The same error a key nobody has ever set would give. A distinguishable answer
        # would confirm that another account has a field by this name.
        with pytest.raises(FieldNotFoundError):
            await fields.forget(
                ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
            )

    async def test_one_account_cannot_list_anothers_fields(self, fields: FieldStore) -> None:
        await set_field(fields, account_id=OTHER_ACCOUNT)

        page = await fields.list_for_persona(ACCOUNT, PROFILE, filters=FieldFilters(), limit=20)

        assert page.items == []

    async def test_one_profile_cannot_read_anothers_field(self, fields: FieldStore) -> None:
        # Two assistants for one person are different people.
        await set_field(fields, profile="home")

        assert await fields.get(ACCOUNT, PROFILE, "voice") is None

    async def test_setting_the_same_key_in_two_personas_makes_two_fields(
        self, fields: FieldStore
    ) -> None:
        await set_field(fields, profile=PROFILE, value="at work")
        await set_field(fields, profile="home", value="at home")

        at_work = await fields.get(ACCOUNT, PROFILE, "voice")
        at_home = await fields.get(ACCOUNT, "home", "voice")

        assert at_work is not None
        assert at_home is not None
        assert at_work.value != at_home.value


class TestCaps:
    async def test_the_field_cap_holds(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=3,
            max_pinned=20,
        )
        for index in range(3):
            await set_field(store, key=f"key{index}")

        with pytest.raises(LimitExceededError, match="3 fields"):
            await set_field(store, key="one_too_many")

    async def test_the_field_cap_holds_under_concurrent_writes(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # A cap checked by the caller before the write has a window between the count
        # and the write that two concurrent writes both pass. This is the test that
        # makes "enforced inside the transaction" a fact rather than a comment.
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=5,
            max_pinned=20,
        )

        await asyncio.gather(
            *(set_field(store, key=f"key{index:02d}") for index in range(30)),
            return_exceptions=True,
        )

        assert await store.count(ACCOUNT, PROFILE) == 5

    async def test_revising_an_existing_field_is_never_refused_by_the_cap(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # A persona at its cap must still be correctable. Refusing an edit because the
        # persona is full would make the last field written permanent.
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=2,
            max_pinned=20,
        )
        await set_field(store, key="a")
        await set_field(store, key="b")

        revised = await set_field(store, key="a", value="corrected")

        assert revised.value == "corrected"

    async def test_the_pinned_cap_holds(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # Pinned is a token budget, not a preference: every pinned entry goes into the
        # assistant's prompt on every turn.
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=2,
        )
        for index in range(2):
            await set_field(store, key=f"key{index}", pinned=True)

        with pytest.raises(LimitExceededError, match="pinned"):
            await set_field(store, key="one_too_many", pinned=True)

    async def test_the_pinned_cap_holds_under_concurrent_writes(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=3,
        )

        await asyncio.gather(
            *(set_field(store, key=f"key{index:02d}", pinned=True) for index in range(20)),
            return_exceptions=True,
        )

        assert len(await store.pinned(ACCOUNT, PROFILE)) == 3

    async def test_pinning_an_existing_field_is_capped_too(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # The cap has to hold on both routes to a pinned row -- creating one pinned, and
        # pinning one that already exists -- or the route that is not checked is the one
        # everybody uses.
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=1,
        )
        await set_field(store, key="already_pinned", pinned=True)
        await set_field(store, key="not_yet")

        with pytest.raises(LimitExceededError, match="pinned"):
            await set_field(store, key="not_yet", value="changed", pinned=True)

    async def test_unpinning_a_field_does_not_need_room(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=1,
        )
        await set_field(store, key="pinned_one", pinned=True)

        unpinned = await set_field(store, key="pinned_one", value="changed", pinned=False)

        assert unpinned.pinned is False

    async def test_an_unpinned_write_is_not_refused_by_a_full_pin_budget(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=1,
        )
        await set_field(store, key="pinned_one", pinned=True)

        ordinary = await set_field(store, key="ordinary")

        assert ordinary.pinned is False

    async def test_a_per_call_pin_ceiling_wins_over_the_constructor(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        # The constructor value is the deployment backstop. A request that carries a
        # narrower cap for this person must be held to that, not to the process-wide 20.
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=20,
        )
        await set_field(store, key="one", pinned=True, max_pinned=1)

        with pytest.raises(LimitExceededError, match="at most 1 pinned"):
            await set_field(store, key="two", pinned=True, max_pinned=1)

    async def test_two_accounts_can_have_different_pin_ceilings_on_the_same_store(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(),
            max_fields=500,
            max_pinned=20,
        )

        await asyncio.gather(
            set_field(store, account_id=ACCOUNT, key="a1", pinned=True, max_pinned=1),
            set_field(store, account_id=ACCOUNT, key="a2", pinned=True, max_pinned=1),
            set_field(store, account_id=OTHER_ACCOUNT, key="b1", pinned=True, max_pinned=3),
            set_field(store, account_id=OTHER_ACCOUNT, key="b2", pinned=True, max_pinned=3),
            set_field(store, account_id=OTHER_ACCOUNT, key="b3", pinned=True, max_pinned=3),
            set_field(store, account_id=OTHER_ACCOUNT, key="b4", pinned=True, max_pinned=3),
            return_exceptions=True,
        )

        assert len(await store.pinned(ACCOUNT, PROFILE)) == 1
        assert len(await store.pinned(OTHER_ACCOUNT, PROFILE)) == 3

    async def test_a_value_over_a_limit_is_refused_naming_it(
        self, database: Database, clock: FakeClock, events: SqlEventLog
    ) -> None:
        store = FieldStore(
            database=database,
            clock=clock,
            events=events,
            limits=ValueLimits(max_bytes=16),
            max_fields=500,
            max_pinned=20,
        )

        with pytest.raises(InvalidFieldValueError, match="bytes"):
            await set_field(store, value="x" * 100)


class TestEvents:
    async def test_creating_a_field_records_that_it_was_set(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert [event.action for event in recorded] == [EventAction.FIELD_SET]
        assert recorded[0].subject == "voice"

    async def test_revising_records_a_revision(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)
        await set_field(fields, value="changed")

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert [event.action for event in recorded] == [
            EventAction.FIELD_REVISED,
            EventAction.FIELD_SET,
        ]

    async def test_an_unchanged_write_records_nothing(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields)
        await set_field(fields)

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert len(recorded) == 1

    async def test_forgetting_records_it(self, fields: FieldStore, events: SqlEventLog) -> None:
        await set_field(fields)
        await fields.forget(
            ACCOUNT, PROFILE, "voice", source=Source.ASSISTANT, asserted_by=ASSERTED_BY
        )

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert recorded[0].action is EventAction.FIELD_FORGOTTEN

    async def test_a_refused_write_records_no_event(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        # The write and its event are one transaction, so a refusal un-does both. An
        # event log that recorded attempts would say a field was set that never was.
        with pytest.raises(CredentialRefusedError):
            await set_field(fields, value="AKIA" + "IOSFODNN7EXAMPLE")

        assert await events.count() == 0

    async def test_no_event_ever_carries_the_value(
        self, fields: FieldStore, events: SqlEventLog
    ) -> None:
        await set_field(fields, value="a distinctive secret-ish phrase")

        recorded, _ = await events.recent(ACCOUNT, PROFILE)

        assert "distinctive" not in recorded[0].detail
        assert "distinctive" not in recorded[0].subject
