"""The four settings that change what happens, over HTTP, for the person whose token it is.

``erasure_mode`` and ``grace_days`` decide what ``forget_field`` and ``forget_note`` do;
``log_values`` decides whether the change log keeps what a change replaced;
``default_persona`` decides what ``@default`` names. Each is checked from both sides:
what a person who chose it gets, and that a person who chose nothing gets exactly what
persona-api did before any of them existed.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from settings_client import HttpSettingsClient
from settings_client.testing import FakeSettingsClient

from persona_api.api.app import create_app
from persona_api.core.container import Container
from persona_api.core.preferences import REFUSED, build_preference_source
from persona_api.domain.erasure import MAX_GRACE_DAYS
from tests.conftest import build_settings
from tests.integration.conftest import auth, token_for, wire_fake_keyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI
    from settings_client import SettingsClient

    from persona_api.core.config import Settings
    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring

FIELD = {"description": "how it speaks", "value": "dry"}
PROFILE_RULE = "a profile name must be lowercase letters"


@pytest.fixture
def chosen() -> SettingsClient:
    return FakeSettingsClient()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
async def app(
    settings: Settings, keyring: FakeKeyring, clock: FakeClock, chosen: SettingsClient
) -> AsyncIterator[FastAPI]:
    """The app on the test's clock, so a grace period can be waited out without waiting."""
    built = create_app(
        settings,
        container_factory=partial(
            Container.build,
            clock=clock,
            preferences=build_preference_source(settings, client=chosen),
        ),
    )
    async with LifespanManager(built):
        wire_fake_keyring(built, keyring, clock)
        yield built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://persona.test"
    ) as http:
        yield http


@pytest.fixture
def headers(keyring: FakeKeyring) -> dict[str, str]:
    """A token that outlives every grace period here, because these tests move the clock."""
    return auth(token_for(keyring, ttl_seconds=timedelta(days=1000).total_seconds()))


async def sweep(app: FastAPI) -> int:
    swept: int = await app.state.container.sweeper.sweep_once()
    return swept


async def write_and_forget(client: AsyncClient, headers: dict[str, str]) -> httpx.Response:
    written = await client.put("/v1/personas/work/fields/voice", json=FIELD, headers=headers)
    assert written.status_code == 200, written.text
    return await client.delete("/v1/personas/work/fields/voice", headers=headers)


async def forgotten_field(client: AsyncClient, headers: dict[str, str]) -> httpx.Response:
    return await client.get(
        "/v1/personas/work/fields/voice?include_forgotten=true", headers=headers
    )


class TestNobodyChose:
    async def test_forgetting_is_still_a_tombstone_however_long_it_waits(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        clock: FakeClock,
    ) -> None:
        """The bug, named: settings-api changed forgetting for a person who chose nothing."""
        assert (await write_and_forget(client, headers)).status_code == 204
        clock.advance(timedelta(days=MAX_GRACE_DAYS + 1))

        assert await sweep(app) == 0
        kept = await forgotten_field(client, headers)
        assert kept.status_code == 200
        assert kept.json()["purge_after"] is None

    async def test_the_log_keeps_no_values(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        await client.put("/v1/personas/work/fields/voice", json=FIELD, headers=headers)
        await client.put(
            "/v1/personas/work/fields/voice", json={**FIELD, "value": "warm"}, headers=headers
        )

        events = (await client.get("/v1/personas/work/events", headers=headers)).json()["events"]

        assert [event["old_value"] for event in events] == [None, None, None]

    async def test_default_names_nothing_and_is_refused_as_it_always_was(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.get("/v1/personas/@default", headers=headers)

        assert response.status_code == 422
        assert PROFILE_RULE in response.json()["detail"]


class TestWithoutSettingsApi:
    """The suite's ordinary app: no settings-api at all, which is most deployments."""

    async def test_everything_is_exactly_as_it_was(
        self, keyring: FakeKeyring, clock: FakeClock, tmp_path: Path
    ) -> None:
        built = create_app(build_settings(tmp_path))
        headers = auth(token_for(keyring))
        async with (
            LifespanManager(built),
            AsyncClient(transport=ASGITransport(app=built), base_url="http://persona.test") as http,
        ):
            wire_fake_keyring(built, keyring, clock)
            assert (await write_and_forget(http, headers)).status_code == 204
            kept = await forgotten_field(http, headers)
            refused = await http.get("/v1/personas/@default/fields", headers=headers)

        assert kept.status_code == 200
        assert kept.json()["purge_after"] is None
        assert refused.status_code == 422


class TestAGracePeriod:
    async def test_the_forgotten_field_says_when_and_goes_then(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        chosen: FakeSettingsClient,
        clock: FakeClock,
    ) -> None:
        chosen.seed("persona", {"erasure_mode": "grace", "grace_days": 7})
        assert (await write_and_forget(client, headers)).status_code == 204

        waiting = await forgotten_field(client, headers)
        assert waiting.status_code == 200
        assert waiting.json()["purge_after"] is not None

        clock.advance(timedelta(days=7))
        assert await sweep(app) == 1
        assert (await forgotten_field(client, headers)).status_code == 404

    async def test_a_note_goes_the_same_way(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        chosen: FakeSettingsClient,
        clock: FakeClock,
    ) -> None:
        chosen.seed("persona", {"erasure_mode": "grace", "grace_days": 1})
        note = await client.post(
            "/v1/personas/work/notes", json={"body": "a thing that happened"}, headers=headers
        )
        note_id = note.json()["note_id"]
        assert (
            await client.delete(f"/v1/personas/work/notes/{note_id}", headers=headers)
        ).status_code == 204

        clock.advance(timedelta(days=1))
        await sweep(app)

        gone = await client.get(
            f"/v1/personas/work/notes/{note_id}?include_forgotten=true", headers=headers
        )
        assert gone.status_code == 404

    async def test_setting_the_field_again_keeps_it(
        self,
        app: FastAPI,
        client: AsyncClient,
        headers: dict[str, str],
        chosen: FakeSettingsClient,
        clock: FakeClock,
    ) -> None:
        chosen.seed("persona", {"erasure_mode": "grace", "grace_days": 7})
        await write_and_forget(client, headers)
        revived = await client.put("/v1/personas/work/fields/voice", json=FIELD, headers=headers)

        clock.advance(timedelta(days=30))

        assert revived.json()["purge_after"] is None
        assert await sweep(app) == 0
        assert (
            await client.get("/v1/personas/work/fields/voice", headers=headers)
        ).status_code == 200


class TestImmediate:
    async def test_the_field_is_gone_when_the_response_arrives(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"erasure_mode": "immediate"})

        assert (await write_and_forget(client, headers)).status_code == 204

        assert (await forgotten_field(client, headers)).status_code == 404
        events = (await client.get("/v1/personas/work/events", headers=headers)).json()["events"]
        assert events[0]["action"] == "field.forgotten"
        assert events[0]["detail"] == "erased"


class TestLoggedValues:
    async def test_a_revision_keeps_the_value_it_replaced_until_the_field_is_destroyed(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"log_values": True})
        await client.put(
            "/v1/personas/work/fields/voice", json={**FIELD, "value": ["dry"]}, headers=headers
        )
        await client.put(
            "/v1/personas/work/fields/voice", json={**FIELD, "value": "warm"}, headers=headers
        )

        logged = (await client.get("/v1/personas/work/events", headers=headers)).json()
        assert logged["events"][0]["old_value"] == {"value": ["dry"]}

        chosen.seed("persona", {"erasure_mode": "immediate"})
        await client.delete("/v1/personas/work/fields/voice", headers=headers)

        after = (await client.get("/v1/personas/work/events", headers=headers)).json()
        assert [event["old_value"] for event in after["events"]] == [None] * len(after["events"])

    async def test_a_note_revision_keeps_the_body_it_replaced(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"log_values": True})
        note = await client.post(
            "/v1/personas/work/notes", json={"body": "the first telling"}, headers=headers
        )
        await client.patch(
            f"/v1/personas/work/notes/{note.json()['note_id']}",
            json={"body": "the second telling"},
            headers=headers,
        )

        logged = (await client.get("/v1/personas/work/events", headers=headers)).json()

        assert logged["events"][0]["old_value"] == {"value": "the first telling"}


class TestTheDefaultPersona:
    async def test_default_names_the_persona_the_person_chose_on_every_route(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"default_persona": "home"})

        written = await client.put(
            "/v1/personas/@default/fields/voice", json=FIELD, headers=headers
        )
        card = await client.get("/v1/personas/@default", headers=headers)
        direct = await client.get("/v1/personas/home/fields/voice", headers=headers)

        assert written.status_code == 200, written.text
        assert card.json()["persona"]["profile"] == "home"
        assert direct.json()["value"] == "dry"
        # Which persona is asked with none named; that persona's own settings with it.
        assert ("persona", None) in chosen.asked
        assert ("persona", "home") in chosen.asked

    async def test_a_default_chosen_for_one_persona_is_not_a_default(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        """A per-profile default_persona is circular, so it is never what @default names.

        Which persona to load is asked before there is one to name, so only an
        account-wide choice can answer it. One stored against a profile is refused with
        the same 422 as no choice at all, rather than half-working.
        """
        chosen.seed("persona", {"default_persona": "home"}, profile="home")

        response = await client.get("/v1/personas/@default", headers=headers)

        assert response.status_code == 422
        assert PROFILE_RULE in response.json()["detail"]

    async def test_any_case_of_the_segment_is_the_same_segment(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"default_persona": "home"})
        await client.put("/v1/personas/home/fields/voice", json=FIELD, headers=headers)

        response = await client.get("/v1/personas/@Default/fields/voice", headers=headers)

        assert response.status_code == 200

    async def test_an_outage_fails_loudly_rather_than_loading_some_persona(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"default_persona": "home"})
        chosen.unavailable = True

        response = await client.get("/v1/personas/@default", headers=headers)

        assert response.status_code == 422

    async def test_settings_api_refusing_this_service_is_a_503_not_a_guess(
        self, client: AsyncClient, headers: dict[str, str], chosen: FakeSettingsClient
    ) -> None:
        chosen.rejects["persona"] = (403, "persona-api was not granted persona")

        response = await client.get("/v1/personas/@default", headers=headers)

        assert response.status_code == 503
        assert response.json()["detail"] == REFUSED


class AccountScopedDefault:
    """settings-api answering as it would with ``default_persona`` account-scoped.

    The value comes back whatever profile is asked for; ``recall_default_limit`` is
    profile-scoped, so the limit depends on which profile the read names.
    """

    def __init__(self) -> None:
        self.profiles_asked: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        profile = request.url.params.get("profile")
        self.profiles_asked.append(profile)
        rules: dict[str, Any] = {"on_unavailable": "use_default"}
        return httpx.Response(
            200,
            headers={"ETag": f'"acct.{profile}"'},
            json={
                "namespace": "persona",
                "revision": 1,
                "settings": {
                    "default_persona": "home",
                    "recall_default_limit": 2 if profile == "home" else 20,
                },
                "fallbacks": {
                    "default_persona": {"default": None, **rules},
                    "recall_default_limit": {"default": 20, **rules},
                },
            },
        )


class TestTheDefaultPersonasOwnSettings:
    @pytest.fixture
    def settings_api(self) -> AccountScopedDefault:
        return AccountScopedDefault()

    @pytest.fixture
    def chosen(self, settings_api: AccountScopedDefault) -> HttpSettingsClient:
        return HttpSettingsClient(
            base_url="http://settings.test",
            service_token="s" * 40,
            transport=httpx.MockTransport(settings_api),
        )

    async def test_the_default_is_asked_for_first_and_then_that_personas_settings(
        self, client: AsyncClient, headers: dict[str, str], settings_api: AccountScopedDefault
    ) -> None:
        for index in range(3):
            written = await client.put(
                f"/v1/personas/home/fields/key{index}",
                json={"description": "n", "value": str(index)},
                headers=headers,
            )
            assert written.status_code == 200, written.text

        page = await client.get("/v1/personas/@default/fields", headers=headers)

        assert page.status_code == 200, page.text
        assert len(page.json()["fields"]) == 2, "the home persona's own page size"
        assert {None, "home"} <= set(settings_api.profiles_asked)
