"""What survives stopping the service and starting it again.

Each test runs two apps in sequence over one database file, exactly as a restart does.
The harness is copied from ``Keyring-api/tests/integration/test_restart.py``.

The assertion worth reading is ``test_searchability_survives``. Everything else here
would pass against a service that rebuilt its full-text index into a temporary table on
every start -- the rows would be there, the listings would be right, and search would
quietly return nothing for anything written before the last restart.

And the negative: **a forgotten note stays forgotten.** A restart that resurrected
tombstoned rows would be worse than one that lost them, because somebody asked for those
to be gone.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from persona_api.api.app import create_app
from tests.conftest import build_settings
from tests.integration.conftest import auth, token_for, wire_fake_keyring
from tests.support.filemode import assert_mode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from persona_api.core.config import Settings
    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring


@pytest.fixture
def durable(tmp_path: Path) -> Settings:
    """One settings object, and therefore one database file, for both runs."""
    return build_settings(tmp_path)


@contextlib.asynccontextmanager
async def running(
    settings: Settings, keyring: FakeKeyring, clock: FakeClock
) -> AsyncIterator[AsyncClient]:
    """One run of the service, lifespan and all, over the given settings.

    Entering it twice with the same settings is a restart: a new container, a new
    connection, and the same file underneath.
    """
    app = create_app(settings)
    async with (
        LifespanManager(app) as managed,
        AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://p.test") as http,
    ):
        wire_fake_keyring(app, keyring, clock)
        yield http


async def test_a_persona_and_its_card_survive(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.post(
            "/v1/personas",
            json={"profile": "work", "display_name": "Ada", "summary": "dry, concise"},
            headers=auth(token),
        )

    async with running(durable, keyring, clock) as second:
        response = await second.get("/v1/personas/work", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["persona"]["display_name"] == "Ada"


async def test_fields_and_notes_survive(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": ["dry", "concise"]},
            headers=auth(token),
        )
        await first.post(
            "/v1/personas/work/notes",
            json={"body": "they went quiet", "kind": "episode"},
            headers=auth(token),
        )

    async with running(durable, keyring, clock) as second:
        field = await second.get("/v1/personas/work/fields/voice", headers=auth(token))
        notes = await second.get("/v1/personas/work/notes", headers=auth(token))

        assert field.json()["value"] == ["dry", "concise"]
        assert [note["body"] for note in notes.json()["notes"]] == ["they went quiet"]


async def test_pins_survive(durable: Settings, keyring: FakeKeyring, clock: FakeClock) -> None:
    # A pin that did not survive would quietly empty the identity block, and the
    # assistant would start a turn knowing nothing about itself.
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry", "pinned": True},
            headers=auth(token),
        )

    async with running(durable, keyring, clock) as second:
        identity = (await second.get("/v1/personas/work", headers=auth(token))).json()

        assert [field["key"] for field in identity["fields"]] == ["voice"]


async def test_searchability_survives(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    # The one that could regress alone. An index rebuilt into a temporary table on every
    # start would pass every other assertion in this file.
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.post(
            "/v1/personas/work/notes",
            json={"body": "a memorable phrase about penguins"},
            headers=auth(token),
        )
        await first.put(
            "/v1/personas/work/fields/favourite_topics",
            json={"description": "what they like", "value": ["jazz", "ambient"]},
            headers=auth(token),
        )

    async with running(durable, keyring, clock) as second:
        found = (await second.get("/v1/recall?q=penguins", headers=auth(token))).json()
        in_a_list = (await second.get("/v1/recall?q=ambient", headers=auth(token))).json()

        assert len(found["notes"]) == 1
        assert len(in_a_list["fields"]) == 1


async def test_the_event_log_survives(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry"},
            headers=auth(token),
        )

    async with running(durable, keyring, clock) as second:
        events = (await second.get("/v1/personas/work/events", headers=auth(token))).json()

        assert "field.set" in [event["action"] for event in events["events"]]


async def test_a_forgotten_note_stays_forgotten(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    # The negative, and the one that matters most: a restart that resurrected tombstoned
    # rows would be worse than one that lost them, because somebody asked for these to
    # be gone.
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        written = await first.post(
            "/v1/personas/work/notes",
            json={"body": "something regretted"},
            headers=auth(token),
        )
        note_id = written.json()["note_id"]
        await first.delete(f"/v1/personas/work/notes/{note_id}", headers=auth(token))

    async with running(durable, keyring, clock) as second:
        read = await second.get(f"/v1/personas/work/notes/{note_id}", headers=auth(token))
        recall = (await second.get("/v1/recall?q=regretted", headers=auth(token))).json()

        assert read.status_code == 404
        assert recall["notes"] == []
        # And still recoverable, because forgetting is soft rather than a deletion.
        restored = await second.get(
            f"/v1/personas/work/notes/{note_id}?include_forgotten=true", headers=auth(token)
        )
        assert restored.status_code == 200


async def test_a_deleted_persona_stays_deleted(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry"},
            headers=auth(token),
        )
        await first.delete("/v1/personas/work", headers=auth(token))

    async with running(durable, keyring, clock) as second:
        assert (await second.get("/v1/personas/work", headers=auth(token))).status_code == 404
        # And its words are not still in the search index, which is what the cascade
        # triggers are for.
        assert (await second.get("/v1/recall?q=dry", headers=auth(token))).json() == {
            "fields": [],
            "notes": [],
        }


async def test_the_database_file_is_still_owner_only_after_a_restart(
    durable: Settings, keyring: FakeKeyring, clock: FakeClock
) -> None:
    # Nothing in this file is encrypted, so the mode is the whole defence -- and a
    # reopen is exactly when a leftover -wal from an unclean shutdown gets its mode
    # applied.
    token = token_for(keyring)
    async with running(durable, keyring, clock) as first:
        await first.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

    async with running(durable, keyring, clock) as second:
        await second.get("/healthy")

    assert_mode(durable.database_path, 0o600)
    for path in durable.database_path.parent.glob("*.db-*"):
        assert_mode(path, 0o600)
