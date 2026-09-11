"""One test per verb: account B never reaches account A's persona.

The answer is always the same 404 account B would get for a profile nobody has ever
used, because a distinguishable answer -- a 403, or a different message -- would tell one
person that another person has a persona. There is no 403 anywhere in this service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration.conftest import auth, token_for

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.fakes.keyring import FakeKeyring

MINE = "acct_mine"
THEIRS = "acct_theirs"


@pytest.fixture
def mine(keyring: FakeKeyring) -> str:
    return token_for(keyring, MINE)


@pytest.fixture
def theirs(keyring: FakeKeyring) -> str:
    return token_for(keyring, THEIRS)


@pytest.fixture
async def their_persona(client: AsyncClient, theirs: str) -> str:
    """A fully furnished persona belonging to somebody else."""
    await client.put(
        "/v1/personas/work/fields/voice",
        json={"description": "how it speaks", "value": "theirs alone", "pinned": True},
        headers=auth(theirs),
    )
    written = await client.post(
        "/v1/personas/work/notes",
        json={"body": "a private thing about them", "pinned": True},
        headers=auth(theirs),
    )
    note_id: str = written.json()["note_id"]
    return note_id


class TestReading:
    async def test_the_identity_block(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        assert their_persona
        response = await client.get("/v1/personas/work", headers=auth(mine))

        assert response.status_code == 404

    async def test_it_is_the_same_answer_as_for_a_profile_nobody_has_used(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        # Byte-identical apart from the request id. A different message would confirm
        # that somebody else has a persona for this profile.
        assert their_persona
        theirs_response = await client.get("/v1/personas/work", headers=auth(mine))
        nobodys = await client.get("/v1/personas/never-used", headers=auth(mine))

        assert theirs_response.status_code == nobodys.status_code == 404
        assert theirs_response.json()["detail"] == nobodys.json()["detail"]

    async def test_listing(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        response = await client.get("/v1/personas", headers=auth(mine))

        assert response.json()["personas"] == []

    async def test_the_schema(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (await client.get("/v1/personas/work/schema", headers=auth(mine))).status_code == 404

    async def test_a_field(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (
            await client.get("/v1/personas/work/fields/voice", headers=auth(mine))
        ).status_code == 404

    async def test_a_note(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert (
            await client.get(f"/v1/personas/work/notes/{their_persona}", headers=auth(mine))
        ).status_code == 404

    async def test_listing_fields(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (await client.get("/v1/personas/work/fields", headers=auth(mine))).status_code == 404

    async def test_listing_notes(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (await client.get("/v1/personas/work/notes", headers=auth(mine))).status_code == 404

    async def test_the_event_log(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (await client.get("/v1/personas/work/events", headers=auth(mine))).status_code == 404

    async def test_the_export(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (await client.get("/v1/personas/work/export", headers=auth(mine))).status_code == 404


class TestSearching:
    async def test_scoped_recall(self, client: AsyncClient, mine: str, their_persona: str) -> None:
        assert their_persona
        assert (
            await client.get("/v1/personas/work/recall?q=private", headers=auth(mine))
        ).status_code == 404

    async def test_recall_across_every_persona_you_own(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        # The one place isolation is not structural: one FTS index holds every
        # account's text, and only the JOIN keeps them apart.
        assert their_persona
        response = await client.get("/v1/recall?q=private", headers=auth(mine))

        assert response.status_code == 200
        assert response.json() == {"fields": [], "notes": []}

    async def test_a_word_both_accounts_used_returns_only_your_own(
        self, client: AsyncClient, mine: str, theirs: str
    ) -> None:
        await client.post(
            "/v1/personas/work/notes",
            json={"body": "the migration review went well"},
            headers=auth(mine),
        )
        await client.post(
            "/v1/personas/work/notes",
            json={"body": "the migration review went badly"},
            headers=auth(theirs),
        )

        found = (await client.get("/v1/recall?q=migration", headers=auth(mine))).json()

        assert [note["body"] for note in found["notes"]] == ["the migration review went well"]


class TestWriting:
    async def test_setting_a_field_makes_your_own_persona_not_theirs(
        self, client: AsyncClient, mine: str, theirs: str, their_persona: str
    ) -> None:
        # A PUT to the same path is a write to a *different* persona, because the
        # account is half the key. It must not touch theirs.
        assert their_persona
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "mine"},
            headers=auth(mine),
        )

        theirs_field = await client.get("/v1/personas/work/fields/voice", headers=auth(theirs))

        assert theirs_field.json()["value"] == "theirs alone"

    async def test_revising_their_note(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        response = await client.patch(
            f"/v1/personas/work/notes/{their_persona}",
            json={"body": "tampered with"},
            headers=auth(mine),
        )

        assert response.status_code == 404

    async def test_forgetting_their_field(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        assert their_persona
        assert (
            await client.delete("/v1/personas/work/fields/voice", headers=auth(mine))
        ).status_code == 404

    async def test_forgetting_their_note(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        assert (
            await client.delete(f"/v1/personas/work/notes/{their_persona}", headers=auth(mine))
        ).status_code == 404

    async def test_updating_their_card(
        self, client: AsyncClient, mine: str, their_persona: str
    ) -> None:
        assert their_persona
        response = await client.patch(
            "/v1/personas/work", json={"display_name": "mine now"}, headers=auth(mine)
        )

        assert response.status_code == 404

    async def test_deleting_their_persona(
        self, client: AsyncClient, mine: str, theirs: str, their_persona: str
    ) -> None:
        assert their_persona
        deleted = await client.delete("/v1/personas/work", headers=auth(mine))

        assert deleted.status_code == 404
        # And theirs is still there, which is the half that would be catastrophic.
        assert (await client.get("/v1/personas/work", headers=auth(theirs))).status_code == 200


class TestThereIsNoWayToNameAnotherAccount:
    async def test_no_route_accepts_an_account_id(self, client: AsyncClient) -> None:
        # Structural rather than checked: the account comes from the token's `sub` and
        # there is no parameter anywhere that could carry another one.
        schema = (await client.get("/openapi.json")).json()

        named = [
            parameter["name"]
            for path in schema["paths"].values()
            for operation in path.values()
            for parameter in operation.get("parameters", [])
        ]
        assert not [name for name in named if "account" in name.lower()]

    async def test_no_request_body_accepts_an_account_id_or_an_asserted_by(
        self, client: AsyncClient
    ) -> None:
        schema = (await client.get("/openapi.json")).json()

        for name, model in schema["components"]["schemas"].items():
            if not name.endswith("Request"):
                continue
            fields = set(model.get("properties", {}))
            assert "account_id" not in fields, name
            # asserted_by is server-derived. A body field claiming otherwise would be
            # the provenance guarantee undone by a single accepted key.
            assert "asserted_by" not in fields, name
