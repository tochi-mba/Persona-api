"""The HTTP contract, pinned.

Operation ids become MCP tool names, so renaming one is a breaking change for every
client with a tool bound to it. The set is written out here rather than left to drift,
and the descriptions are checked because they are what a model reads to decide whether
and how to call something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration.conftest import auth, token_for

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.fakes.keyring import FakeKeyring

OPERATION_IDS = {
    # health
    "get_health",
    "check_readiness",
    # personas
    "list_personas",
    "create_persona",
    "get_persona",
    "update_persona",
    "delete_persona",
    "describe_persona_schema",
    # fields
    "list_fields",
    "set_field",
    "get_field",
    "forget_field",
    # notes
    "list_notes",
    "write_note",
    "get_note",
    "revise_note",
    "forget_note",
    # retrieval
    "recall",
    "recall_everywhere",
    "export_persona",
    "read_persona_events",
}


class TestOperationIds:
    async def test_the_set_is_exactly_this(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        found = {
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
        }
        assert found == OPERATION_IDS

    async def test_every_operation_describes_itself_for_a_model(self, client: AsyncClient) -> None:
        # These become tool descriptions. An undescribed route cannot ship.
        schema = (await client.get("/openapi.json")).json()

        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                assert operation.get("summary"), f"{method} {path} has no summary"
                assert len(operation.get("description", "")) > 40, f"{method} {path}"

    async def test_the_dangerous_operation_says_it_is_dangerous(self, client: AsyncClient) -> None:
        # delete_persona is the only hard delete and it cascades. A model reading the
        # tool description has to be told that before it calls it, not after.
        schema = (await client.get("/openapi.json")).json()

        description = schema["paths"]["/v1/personas/{profile}"]["delete"]["description"]

        assert "HARD DELETE" in description
        assert "no undo" in description.lower()

    async def test_the_anti_sprawl_endpoint_says_what_it_is_for(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        description = schema["paths"]["/v1/personas/{profile}/schema"]["get"]["description"]

        assert "BEFORE inventing" in description


class TestFailuresAreDocumented:
    @pytest.mark.parametrize(
        ("path", "method", "code"),
        [
            ("/v1/personas", "get", "401"),
            ("/v1/personas", "get", "503"),
            ("/v1/personas", "post", "409"),
            ("/v1/personas/{profile}", "get", "404"),
            ("/v1/personas/{profile}/fields/{key}", "put", "422"),
            ("/v1/personas/{profile}/fields/{key}", "put", "429"),
        ],
    )
    async def test_a_failure_a_caller_can_provoke_is_declared(
        self, client: AsyncClient, path: str, method: str, code: str
    ) -> None:
        schema = (await client.get("/openapi.json")).json()

        assert code in schema["paths"][path][method]["responses"]


class TestProvenanceIsNotOptional:
    @pytest.mark.parametrize("model", ["FieldResponse", "NoteResponse"])
    async def test_every_memory_model_carries_its_provenance(
        self, client: AsyncClient, model: str
    ) -> None:
        # Walking the declared contract rather than one sampled response, so a lighter
        # variant added later is caught here rather than by whoever notices that
        # memories have started arriving unattributed.
        schema = (await client.get("/openapi.json")).json()
        fields = schema["components"]["schemas"][model]["properties"]

        assert {"source", "asserted_by", "revision", "created_at", "updated_at"} <= set(fields)

    @pytest.mark.parametrize("model", ["FieldResponse", "NoteResponse"])
    async def test_the_source_field_says_it_is_unverified(
        self, client: AsyncClient, model: str
    ) -> None:
        # A model reading the tool schema is told the field's epistemic status at the
        # same moment it is told the field exists. That is the whole mitigation.
        schema = (await client.get("/openapi.json")).json()

        description = schema["components"]["schemas"][model]["properties"]["source"]["description"]

        assert "CLAIM" in description
        assert "not verified" in description

    @pytest.mark.parametrize("model", ["FieldResponse", "NoteResponse"])
    async def test_the_asserted_by_field_says_it_is_server_derived(
        self, client: AsyncClient, model: str
    ) -> None:
        schema = (await client.get("/openapi.json")).json()

        description = schema["components"]["schemas"][model]["properties"]["asserted_by"][
            "description"
        ]

        assert "Server-derived" in description


class TestNoResponseCanCarryACredential:
    async def test_no_response_schema_has_a_credential_shaped_field(
        self, client: AsyncClient
    ) -> None:
        # This service refuses credentials on the way in. This is the other half:
        # nothing on the way out is shaped to hold one either, checked against the
        # declared contract so a field added later is caught by the suite.
        schema = (await client.get("/openapi.json")).json()

        forbidden = ("password", "secret", "token", "api_key", "private_key", "credential")
        offenders = [
            f"{name}.{field}"
            for name, model in schema["components"]["schemas"].items()
            if name.endswith("Response")
            for field in model.get("properties", {})
            if any(bad in field.lower() for bad in forbidden)
        ]

        assert offenders == []


class TestRequestModelsRefuseInventedFields:
    @pytest.mark.parametrize(
        "model",
        [
            "CreatePersonaRequest",
            "UpdatePersonaRequest",
            "SetFieldRequest",
            "WriteNoteRequest",
            "ReviseNoteRequest",
        ],
    )
    async def test_extra_fields_are_forbidden(self, client: AsyncClient, model: str) -> None:
        # extra="forbid", so an invented field is a 422 rather than being ignored --
        # which is what stops `asserted_by` in a body from looking like it worked.
        schema = (await client.get("/openapi.json")).json()

        assert schema["components"]["schemas"][model].get("additionalProperties") is False


class TestExamples:
    @pytest.mark.parametrize(
        "model", ["CreatePersonaRequest", "SetFieldRequest", "WriteNoteRequest"]
    )
    async def test_request_bodies_carry_an_example(self, client: AsyncClient, model: str) -> None:
        # A model decides how to call a tool from its schema, and an example is worth
        # more than three field descriptions.
        schema = (await client.get("/openapi.json")).json()

        assert schema["components"]["schemas"][model]["examples"]


class TestHealthIsTheOnlyUnauthenticatedRoute:
    async def test_health_needs_no_token(self, client: AsyncClient) -> None:
        assert (await client.get("/healthy")).status_code == 200

    async def test_readiness_needs_no_token(self, client: AsyncClient) -> None:
        assert (await client.get("/ready")).status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/personas",
            "/v1/personas/work",
            "/v1/personas/work/fields",
            "/v1/personas/work/notes",
            "/v1/personas/work/recall?q=x",
            "/v1/recall?q=x",
            "/v1/personas/work/export",
            "/v1/personas/work/events",
            "/v1/personas/work/schema",
        ],
    )
    async def test_every_other_route_refuses_an_anonymous_caller(
        self, client: AsyncClient, path: str
    ) -> None:
        response = await client.get(path)

        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_health_reports_no_personal_data(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # Unauthenticated, so it is written assuming a stranger is reading it.
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "a distinctive value"},
            headers=auth(token_for(keyring)),
        )

        body = (await client.get("/ready")).text

        assert "distinctive" not in body
        assert "work" not in body
        assert "voice" not in body
