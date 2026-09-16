"""The failure paths: one error shape, and no detail that should not leave the process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from persona_api.api.app import create_app
from persona_api.api.middleware import REQUEST_ID_HEADER, RESPONSE_TIME_HEADER
from tests.integration.conftest import auth, token_for, wire_fake_keyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_api.core.config import Settings
    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring

SECRET_ISH = "/var/lib/persona/persona.db and the key sk-live-abcdef"


@pytest.fixture
async def exploding(
    settings: Settings, keyring: FakeKeyring, clock: FakeClock
) -> AsyncIterator[AsyncClient]:
    """An app with one route that raises, to exercise the unhandled-error path.

    Mounted in the test rather than shipped, obviously -- but mounted on the *real* app
    so the response goes through the real middleware, the real handlers and the real
    request-id binding. A unit test of the handler function would not prove that the id
    is still bound when it runs, which is the entire reason it lives where it does.
    """
    app = create_app(settings)
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> dict[str, Any]:
        raise RuntimeError(SECRET_ISH)

    app.include_router(router)
    async with (
        LifespanManager(app) as managed,
        AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://p.test") as http,
    ):
        wire_fake_keyring(app, keyring, clock)
        yield http


class TestAnUnexpectedFailure:
    async def test_it_is_a_five_hundred_in_the_one_error_shape(
        self, exploding: AsyncClient
    ) -> None:
        response = await exploding.get("/boom")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/problem+json")
        assert set(response.json()) >= {"type", "title", "status", "detail", "request_id"}

    async def test_the_exception_text_never_reaches_the_caller(
        self, exploding: AsyncClient
    ) -> None:
        # It can carry a filesystem path, an internal hostname, or a fragment of the
        # very credential this service just refused to store.
        response = await exploding.get("/boom")

        assert "persona.db" not in response.text
        assert "sk-live" not in response.text
        assert "RuntimeError" not in response.text

    async def test_it_carries_a_request_id_to_quote(self, exploding: AsyncClient) -> None:
        # Handled inside the middleware, while the id is still bound. From Starlette's
        # outermost error middleware the binding has already unwound and the response
        # would carry no id at all -- which is the one thing the caller is told to quote.
        response = await exploding.get("/boom")

        assert response.json()["request_id"]
        assert response.headers[REQUEST_ID_HEADER] == response.json()["request_id"]


class TestRequestIds:
    async def test_every_response_carries_one(self, client: AsyncClient) -> None:
        response = await client.get("/healthy")

        assert response.headers[REQUEST_ID_HEADER]
        assert response.headers[RESPONSE_TIME_HEADER]

    async def test_a_supplied_one_is_honoured_so_a_trace_can_span_services(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/healthy", headers={REQUEST_ID_HEADER: "from-upstream"})

        assert response.headers[REQUEST_ID_HEADER] == "from-upstream"

    async def test_a_supplied_one_is_length_capped(self, client: AsyncClient) -> None:
        # It ends up in every log record for this request, so a caller cannot use it to
        # write a megabyte into the log pipeline.
        response = await client.get("/healthy", headers={REQUEST_ID_HEADER: "x" * 500})

        assert len(response.headers[REQUEST_ID_HEADER]) < 500


class TestRoutingFailures:
    async def test_an_unknown_path_is_a_problem_response_too(self, client: AsyncClient) -> None:
        # Starlette's own 404. Without the handler it would be a bare JSON body in a
        # different shape from every other failure this service produces.
        response = await client.get("/v1/nothing-here")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["title"] == "Not found"

    async def test_a_wrong_method_is_one_as_well(self, client: AsyncClient) -> None:
        response = await client.post("/healthy")

        assert response.status_code == 405
        assert response.headers["content-type"].startswith("application/problem+json")


class TestValidationFailures:
    async def test_the_offending_input_is_not_echoed_back(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # FastAPI's raw validation errors include the input, and on this service that
        # input is routinely the thing somebody asked to have remembered.
        response = await client.post(
            "/v1/personas/work/notes",
            json={"body": "a memorable phrase", "kind": "not-a-kind"},
            headers=auth(token_for(keyring)),
        )

        assert response.status_code == 422
        assert "a memorable phrase" not in response.text

    async def test_it_says_which_field_was_wrong(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        response = await client.post(
            "/v1/personas/work/notes",
            json={"body": "x", "kind": "not-a-kind"},
            headers=auth(token_for(keyring)),
        )

        assert any("kind" in error["location"] for error in response.json()["errors"])


class TestMissingRowsInsideAPersonaThatExists:
    async def test_a_field_that_was_never_set_is_a_clean_404(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        token = token_for(keyring)
        await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

        response = await client.get("/v1/personas/work/fields/never_set", headers=auth(token))

        assert response.status_code == 404

    async def test_a_note_that_was_never_written_is_a_clean_404(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        token = token_for(keyring)
        await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

        response = await client.get("/v1/personas/work/notes/note_nothing", headers=auth(token))

        assert response.status_code == 404

    async def test_a_forgotten_field_is_the_same_404_as_one_never_set(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        token = token_for(keyring)
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry"},
            headers=auth(token),
        )
        await client.delete("/v1/personas/work/fields/voice", headers=auth(token))

        forgotten = await client.get("/v1/personas/work/fields/voice", headers=auth(token))
        never = await client.get("/v1/personas/work/fields/never_set", headers=auth(token))

        assert forgotten.status_code == never.status_code == 404
        assert forgotten.json()["detail"] == never.json()["detail"]
