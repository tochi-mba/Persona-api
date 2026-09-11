"""Fixtures for the persona layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.domain.fields import ValueLimits
from persona_api.events.sql_log import SqlEventLog
from persona_api.memory.fields import FieldStore
from persona_api.memory.notes import NoteStore
from persona_api.personas.service import PersonaService
from persona_api.personas.store import PersonaStore
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

    from persona_api.core.config import Settings
    from persona_api.storage.database import Database

ASSERTED_BY = "persona"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def events(database: Database, clock: FakeClock) -> SqlEventLog:
    return SqlEventLog(database=database, clock=clock)


@pytest.fixture
def store(database: Database) -> PersonaStore:
    return PersonaStore(database=database)


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
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


# PLR0917: a fixture takes its dependencies positionally because pytest injects them
# that way; six is what this service composes.
@pytest.fixture
def service(  # noqa: PLR0917
    store: PersonaStore,
    fields: FieldStore,
    notes: NoteStore,
    events: SqlEventLog,
    clock: FakeClock,
    settings: Settings,
) -> PersonaService:
    return PersonaService(
        personas=store,
        fields=fields,
        notes=notes,
        events=events,
        clock=clock,
        settings=settings,
    )
