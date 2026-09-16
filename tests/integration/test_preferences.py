"""Per-person settings over HTTP: what somebody chose is what their writes are held to.

settings-api is its shared fake here, wired in through the composition root the way the
real client is.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from persona_api.api.app import create_app
from persona_api.core.container import Container
from persona_api.core.preferences import REFUSED, build_preference_source
from tests.conftest import OTHER_ACCOUNT, build_settings
from tests.integration.conftest import auth, token_for, wire_fake_keyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from persona_api.core.config import Settings
    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring

FIELD = {
    "description": "how it speaks",
    "value": "dry",
    "pinned": True,
}


class TokenKeyedFake(FakeSettingsClient):
    """A fake that can answer differently for two tokens.

    The shared fake seeds one namespace for everybody. Two accounts having different pin
    ceilings is the property this service has to prove, so the seed is keyed by token here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_token: dict[str, dict[str, Any]] = {}

    def seed_token(self, user_token: str, values: Mapping[str, Any]) -> None:
        self.by_token[user_token] = dict(values)

    async def resolve(self, namespace: str, *, user_token: str) -> Any:
        if user_token in self.by_token:
            self._values[namespace] = dict(self.by_token[user_token])
        return await super().resolve(namespace, user_token=user_token)


@pytest.fixture
def chosen() -> FakeSettingsClient:
    return FakeSettingsClient()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path, max_pinned_fields=5, max_pinned_notes=5, recall_default_limit=4)


@pytest.fixture
async def client(
    settings: Settings,
    keyring: FakeKeyring,
    clock: FakeClock,
    chosen: FakeSettingsClient,
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings,
        container_factory=partial(
            Container.build,
            preferences=build_preference_source(settings, client=chosen),
        ),
    )
    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            transport=ASGITransport(app=managed.app), base_url="http://persona.test"
        ) as http,
    ):
        wire_fake_keyring(app, keyring, clock)
        yield http


@pytest.fixture
def token(keyring: FakeKeyring) -> str:
    return token_for(keyring)


class TestChoicesReachTheWrite:
    async def test_a_narrower_pin_ceiling_is_enforced(
        self, client: AsyncClient, token: str, chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"max_pinned_fields": 1})
        first = await client.put("/v1/personas/work/fields/voice", json=FIELD, headers=auth(token))
        second = await client.put(
            "/v1/personas/work/fields/tone",
            json={**FIELD, "description": "warmth"},
            headers=auth(token),
        )

        assert first.status_code == 200
        assert second.status_code == 429
        assert "at most 1 pinned" in second.json()["detail"]

    async def test_an_omitted_limit_uses_the_persons_default(
        self, client: AsyncClient, token: str, chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"recall_default_limit": 2})
        for index in range(4):
            await client.put(
                f"/v1/personas/work/fields/key{index}",
                json={"description": "n", "value": str(index)},
                headers=auth(token),
            )

        response = await client.get("/v1/personas/work/fields", headers=auth(token))

        assert response.status_code == 200
        assert len(response.json()["fields"]) == 2
        assert response.json()["next_cursor"] is not None

    async def test_a_named_limit_is_still_held_to_the_deployment_maximum(
        self, client: AsyncClient, token: str, chosen: FakeSettingsClient
    ) -> None:
        chosen.seed("persona", {"recall_default_limit": 2})

        response = await client.get("/v1/personas/work/fields?limit=500", headers=auth(token))

        assert response.status_code == 422


class TestWhenSettingsApiIsUnwell:
    async def test_an_outage_leaves_the_deployment_caps(
        self, client: AsyncClient, token: str, chosen: FakeSettingsClient
    ) -> None:
        chosen.unavailable = True

        response = await client.put(
            "/v1/personas/work/fields/voice", json=FIELD, headers=auth(token)
        )

        assert response.status_code == 200

    async def test_settings_api_refusing_this_service_is_a_503(
        self, client: AsyncClient, token: str, chosen: FakeSettingsClient
    ) -> None:
        chosen.rejects["persona"] = (403, "persona-api was not granted persona")

        response = await client.get("/v1/personas/work/fields", headers=auth(token))

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        problem = response.json()
        assert problem["detail"] == REFUSED
        assert "granted" not in problem["detail"]
        assert "persona-api" not in problem["detail"]


class TestTwoAccounts:
    @pytest.fixture
    def chosen(self) -> TokenKeyedFake:
        return TokenKeyedFake()

    async def test_two_accounts_are_held_to_different_pin_ceilings(
        self,
        client: AsyncClient,
        keyring: FakeKeyring,
        chosen: TokenKeyedFake,
    ) -> None:
        alice = token_for(keyring, "acct_one")
        bob = token_for(keyring, OTHER_ACCOUNT)
        chosen.seed_token(alice, {"max_pinned_fields": 1})
        chosen.seed_token(bob, {"max_pinned_fields": 3})

        assert (
            await client.put("/v1/personas/work/fields/a1", json=FIELD, headers=auth(alice))
        ).status_code == 200
        assert (
            await client.put(
                "/v1/personas/work/fields/a2",
                json={**FIELD, "description": "two"},
                headers=auth(alice),
            )
        ).status_code == 429

        for index in range(3):
            written = await client.put(
                f"/v1/personas/work/fields/b{index}",
                json={**FIELD, "description": f"b{index}"},
                headers=auth(bob),
            )
            assert written.status_code == 200, written.text
        fourth = await client.put(
            "/v1/personas/work/fields/b3",
            json={**FIELD, "description": "b3"},
            headers=auth(bob),
        )
        assert fourth.status_code == 429
