"""An app wired to a keyring that exists inside the test process.

The container builds its own :class:`~persona_api.auth.jwks.JwksClient` pointed at a URL,
which is right for production and useless in a test. So the fixtures below start the app
for real -- lifespan, migrations, middleware and all -- and then substitute a client
whose transport is the fake keyring.

Substituted rather than injected through settings, deliberately: a settings knob that
selected a transport would be a setting that could point token verification at something
else, and this service has no such knob by design. The substitution lives in the test
suite, where it cannot ship.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from persona_api.auth.jwks import JwksClient
from persona_api.auth.verifier import TokenVerifier
from tests.conftest import AUDIENCE, JWKS_URL, KEYRING_ISSUER, build_settings
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import FakeJwksEndpoint, FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from persona_api.core.config import Settings


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def endpoint(keyring: FakeKeyring) -> FakeJwksEndpoint:
    return FakeJwksEndpoint(keyring)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


def wire_fake_keyring(app: FastAPI, endpoint: FakeJwksEndpoint, clock: FakeClock) -> None:
    """Point the running app's verifier at the fake keyring."""
    container = app.state.container
    container.clock = clock
    container.jwks = JwksClient(
        url=JWKS_URL,
        clock=clock,
        cache_seconds=3600.0,
        min_refetch_seconds=60.0,
        timeout_seconds=5.0,
        transport=endpoint.transport(),
    )
    container.verifier = TokenVerifier(
        jwks=container.jwks, issuer=KEYRING_ISSUER, audience=AUDIENCE, clock=clock
    )


@pytest.fixture
async def client(
    settings: Settings, endpoint: FakeJwksEndpoint, clock: FakeClock
) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired straight to the ASGI app, with lifespan run for real."""
    from persona_api.api.app import create_app

    app = create_app(settings)
    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            transport=ASGITransport(app=managed.app), base_url="http://persona.test"
        ) as http,
    ):
        # `app`, not `managed.app`: LifespanManager hands back the wrapped ASGI
        # callable, which has no `.state`. The container we want is on the FastAPI
        # object the factory returned.
        wire_fake_keyring(app, endpoint, clock)
        yield http


def auth(token: str) -> dict[str, str]:
    """The Authorization header for a keyring token."""
    return {"Authorization": f"Bearer {token}"}


def token_for(keyring: FakeKeyring, account: str = "acct_one", **overrides: Any) -> str:
    """Mint a token for one account, exactly as keyring would."""
    return keyring.mint(subject=account, issued_at=EPOCH, **overrides)


def container_of(app: FastAPI) -> Any:
    """Reach the wired container, for tests that inspect or substitute an adapter."""
    return app.state.container
