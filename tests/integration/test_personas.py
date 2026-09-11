"""Personas, fields and notes over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration.conftest import auth, token_for

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.fakes.keyring import FakeKeyring


@pytest.fixture
def token(keyring: FakeKeyring) -> str:
    return token_for(keyring)


class TestPersonaLifecycle:
    async def test_creating_reading_and_deleting(self, client: AsyncClient, token: str) -> None:
        created = await client.post(
            "/v1/personas",
            json={"profile": "work", "display_name": "Ada", "summary": "dry, concise"},
            headers=auth(token),
        )
        assert created.status_code == 201, created.text
        assert created.json()["display_name"] == "Ada"

        read = await client.get("/v1/personas/work", headers=auth(token))
        assert read.status_code == 200
        assert read.json()["persona"]["summary"] == "dry, concise"

        deleted = await client.delete("/v1/personas/work", headers=auth(token))
        assert deleted.status_code == 204

        assert (await client.get("/v1/personas/work", headers=auth(token))).status_code == 404

    async def test_the_same_profile_twice_is_a_conflict(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

        again = await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

        assert again.status_code == 409

    async def test_a_typo_makes_a_second_empty_persona_that_the_listing_reveals(
        self, client: AsyncClient, token: str
    ) -> None:
        # persona-api cannot check a profile against keyring, so this is the honest
        # behaviour rather than a bug -- and list_personas is how anybody notices.
        await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))
        await client.post("/v1/personas", json={"profile": "wrok"}, headers=auth(token))

        listed = await client.get("/v1/personas", headers=auth(token))

        assert [p["profile"] for p in listed.json()["personas"]] == ["work", "wrok"]

    async def test_an_unusable_profile_name_is_refused(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.post(
            "/v1/personas", json={"profile": "not a profile!"}, headers=auth(token)
        )

        assert response.status_code == 422

    async def test_patching_changes_only_what_was_sent(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.post(
            "/v1/personas",
            json={"profile": "work", "display_name": "Ada", "pronouns": "they/them"},
            headers=auth(token),
        )

        patched = await client.patch(
            "/v1/personas/work", json={"summary": "warmer now"}, headers=auth(token)
        )

        assert patched.json()["display_name"] == "Ada"
        assert patched.json()["pronouns"] == "they/them"
        assert patched.json()["summary"] == "warmer now"

    async def test_an_explicit_null_clears_a_card_field(
        self, client: AsyncClient, token: str
    ) -> None:
        # "Clear this" and "leave this alone" are different requests, and the router
        # tells them apart by which keys the body actually contained.
        await client.post(
            "/v1/personas",
            json={"profile": "work", "pronouns": "they/them"},
            headers=auth(token),
        )

        patched = await client.patch(
            "/v1/personas/work", json={"pronouns": None}, headers=auth(token)
        )

        assert patched.json()["pronouns"] is None

    async def test_an_invented_body_field_is_refused_rather_than_ignored(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.post(
            "/v1/personas",
            json={"profile": "work", "asserted_by": "somebody-else"},
            headers=auth(token),
        )

        assert response.status_code == 422


class TestFields:
    async def test_setting_a_field_creates_the_persona(
        self, client: AsyncClient, token: str
    ) -> None:
        # So an assistant can record something without calling create_persona first.
        response = await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry and concise"},
            headers=auth(token),
        )

        assert response.status_code == 200, response.text
        assert (await client.get("/v1/personas/work", headers=auth(token))).status_code == 200

    async def test_the_key_is_normalized(self, client: AsyncClient, token: str) -> None:
        await client.put(
            "/v1/personas/work/fields/Favourite%20Topics",
            json={"description": "what they like", "value": ["jazz"]},
            headers=auth(token),
        )

        read = await client.get("/v1/personas/work/fields/favourite-topics", headers=auth(token))

        assert read.status_code == 200
        assert read.json()["key"] == "favourite_topics"

    async def test_setting_it_twice_leaves_one_field_at_revision_one(
        self, client: AsyncClient, token: str
    ) -> None:
        body = {"description": "how it speaks", "value": "dry"}
        for _ in range(3):
            await client.put("/v1/personas/work/fields/voice", json=body, headers=auth(token))

        read = await client.get("/v1/personas/work/fields/voice", headers=auth(token))

        assert read.json()["revision"] == 1

    async def test_the_value_type_is_derived(self, client: AsyncClient, token: str) -> None:
        response = await client.put(
            "/v1/personas/work/fields/verbose",
            json={"description": "whether it rambles", "value": False},
            headers=auth(token),
        )

        assert response.json()["value_type"] == "boolean"

    async def test_a_credential_value_is_refused_and_names_keyring(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.put(
            "/v1/personas/work/fields/api_key",
            json={"description": "a key", "value": "AKIA" + "IOSFODNN7EXAMPLE"},
            headers=auth(token),
        )

        assert response.status_code == 422
        assert "keyring" in response.json()["detail"]

    async def test_a_pem_block_is_refused(self, client: AsyncClient, token: str) -> None:
        response = await client.post(
            "/v1/personas/work/notes",
            json={
                "body": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----"
            },
            headers=auth(token),
        )

        assert response.status_code == 422
        assert "keyring" in response.json()["detail"]

    async def test_a_missing_description_is_refused(self, client: AsyncClient, token: str) -> None:
        response = await client.put(
            "/v1/personas/work/fields/voice", json={"value": "dry"}, headers=auth(token)
        )

        assert response.status_code == 422

    async def test_forgetting_is_soft_and_reversible(self, client: AsyncClient, token: str) -> None:
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry"},
            headers=auth(token),
        )

        forgotten = await client.delete("/v1/personas/work/fields/voice", headers=auth(token))
        assert forgotten.status_code == 204

        assert (
            await client.get("/v1/personas/work/fields/voice", headers=auth(token))
        ).status_code == 404
        back = await client.get(
            "/v1/personas/work/fields/voice?include_forgotten=true", headers=auth(token)
        )
        assert back.status_code == 200
        assert back.json()["forgotten_at"] is not None

    async def test_the_schema_endpoint_carries_no_values(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "a distinctive value"},
            headers=auth(token),
        )

        response = await client.get("/v1/personas/work/schema", headers=auth(token))

        assert response.json()["fields"][0]["key"] == "voice"
        assert "a distinctive value" not in response.text


class TestNotes:
    async def test_writing_and_reading(self, client: AsyncClient, token: str) -> None:
        written = await client.post(
            "/v1/personas/work/notes",
            json={"body": "they went quiet", "kind": "episode"},
            headers=auth(token),
        )
        assert written.status_code == 201, written.text
        note_id = written.json()["note_id"]

        read = await client.get(f"/v1/personas/work/notes/{note_id}", headers=auth(token))

        assert read.json()["body"] == "they went quiet"

    async def test_two_notes_saying_the_same_thing_are_two_notes(
        self, client: AsyncClient, token: str
    ) -> None:
        body = {"body": "they went quiet"}
        first = await client.post("/v1/personas/work/notes", json=body, headers=auth(token))
        second = await client.post("/v1/personas/work/notes", json=body, headers=auth(token))

        assert first.json()["note_id"] != second.json()["note_id"]

    async def test_revising_changes_only_what_was_sent(
        self, client: AsyncClient, token: str
    ) -> None:
        written = await client.post(
            "/v1/personas/work/notes",
            json={"body": "the original", "kind": "episode"},
            headers=auth(token),
        )
        note_id = written.json()["note_id"]

        revised = await client.patch(
            f"/v1/personas/work/notes/{note_id}", json={"pinned": True}, headers=auth(token)
        )

        assert revised.json()["body"] == "the original"
        assert revised.json()["kind"] == "episode"
        assert revised.json()["pinned"] is True

    async def test_an_invented_kind_is_refused(self, client: AsyncClient, token: str) -> None:
        response = await client.post(
            "/v1/personas/work/notes",
            json={"body": "x", "kind": "lessons"},
            headers=auth(token),
        )

        assert response.status_code == 422


class TestTheIdentityBlock:
    async def test_it_carries_pinned_entries_and_counts(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry", "pinned": True},
            headers=auth(token),
        )
        await client.put(
            "/v1/personas/work/fields/pace",
            json={"description": "how fast", "value": "slow"},
            headers=auth(token),
        )
        await client.post(
            "/v1/personas/work/notes",
            json={"body": "in the prompt", "pinned": True},
            headers=auth(token),
        )

        identity = (await client.get("/v1/personas/work", headers=auth(token))).json()

        assert [field["key"] for field in identity["fields"]] == ["voice"]
        assert identity["field_count"] == 2
        assert identity["note_count"] == 1

    async def test_every_returned_memory_carries_its_provenance(
        self, client: AsyncClient, token: str
    ) -> None:
        # Not an optional expansion: a memory separated from where it came from has
        # lost the thing that lets a reader discount it.
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry", "pinned": True},
            headers=auth(token),
        )

        field = (await client.get("/v1/personas/work", headers=auth(token))).json()["fields"][0]

        assert field["source"] == "assistant"
        assert field["asserted_by"] == "persona"
        assert field["revision"] == 1
        assert field["created_at"]

    async def test_there_is_no_rendered_prose_on_it(self, client: AsyncClient, token: str) -> None:
        # Turning a persona into a system prompt is the MCP layer's job. An endpoint
        # that returned one would be an endpoint that gets pasted into one.
        await client.post("/v1/personas", json={"profile": "work"}, headers=auth(token))

        identity = (await client.get("/v1/personas/work", headers=auth(token))).json()

        assert not {"prompt", "rendered", "text", "system_prompt"} & set(identity)
