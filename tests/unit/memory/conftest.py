"""Fixtures for the memory layer, and the helpers that keep its tests readable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.domain.fields import ValueLimits
from persona_api.domain.notes import NoteKind
from persona_api.domain.provenance import Source
from persona_api.events.sql_log import SqlEventLog
from persona_api.memory.fields import FieldStore
from persona_api.memory.notes import NoteStore
from tests.conftest import ACCOUNT, OTHER_ACCOUNT, PROFILE
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from persona_api.domain.fields import Field
    from persona_api.domain.notes import Note
    from persona_api.storage.database import Database

ASSERTED_BY = "persona"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def events(database: Database, clock: FakeClock) -> SqlEventLog:
    return SqlEventLog(database=database, clock=clock)


@pytest.fixture
def fields(database: Database, clock: FakeClock, events: SqlEventLog) -> FieldStore:
    return FieldStore(
        database=database,
        clock=clock,
        events=events,
        limits=ValueLimits(),
        max_fields=500,
        max_pinned=20,
    )


@pytest.fixture
def notes(database: Database, clock: FakeClock, events: SqlEventLog) -> NoteStore:
    return NoteStore(
        database=database,
        clock=clock,
        events=events,
        max_body_chars=4000,
        max_notes=5000,
        max_pinned=20,
    )


@pytest.fixture
async def personas(database: Database) -> None:
    """The personas every field and note in these tests hangs off.

    Created directly rather than through PersonaService, because these are the store
    tests -- the foreign key is what needs to exist, not the layer above it.
    """
    for account_id in (ACCOUNT, OTHER_ACCOUNT):
        for profile in (PROFILE, "home"):
            await database.execute(
                "INSERT INTO personas (account_id, profile, persona_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    account_id,
                    profile,
                    f"per_{account_id}_{profile}",
                    "2026-01-01T12:00:00.000000+00:00",
                    "2026-01-01T12:00:00.000000+00:00",
                ),
            )


async def set_field(
    store: FieldStore,
    *,
    account_id: str = ACCOUNT,
    profile: str = PROFILE,
    key: str = "voice",
    description: str = "how it speaks",
    value: object = "dry and concise",
    source: Source = Source.ASSISTANT,
    pinned: bool | None = None,
) -> Field:
    return await store.set(
        account_id=account_id,
        profile=profile,
        key=key,
        description=description,
        value=value,
        source=source,
        asserted_by=ASSERTED_BY,
        pinned=pinned,
    )


async def write_note(
    store: NoteStore,
    *,
    account_id: str = ACCOUNT,
    profile: str = PROFILE,
    body: str = "they went quiet when I suggested a rewrite",
    kind: NoteKind = NoteKind.EPISODE,
    source: Source = Source.ASSISTANT,
    pinned: bool = False,
) -> Note:
    return await store.write(
        account_id=account_id,
        profile=profile,
        body=body,
        kind=kind,
        source=source,
        asserted_by=ASSERTED_BY,
        pinned=pinned,
    )
