"""Filters, paging, recall and export over HTTP, plus the hostile search strings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import build_settings
from tests.integration.conftest import auth, token_for
from tests.unit.memory.test_search import HOSTILE

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

    from persona_api.core.config import Settings
    from tests.fakes.keyring import FakeKeyring


@pytest.fixture
def token(keyring: FakeKeyring) -> str:
    return token_for(keyring)


@pytest.fixture
async def furnished(client: AsyncClient, token: str) -> None:
    """A persona with enough in it to filter and page over."""
    for index in range(12):
        await client.put(
            f"/v1/personas/work/fields/key{index:02d}",
            json={
                "description": f"field number {index}",
                "value": f"value {index}",
                "pinned": index < 2,
                "source": "owner" if index % 2 else "assistant",
            },
            headers=auth(token),
        )
    for index in range(12):
        await client.post(
            "/v1/personas/work/notes",
            json={
                "body": f"note number {index}",
                "kind": "lesson" if index % 3 == 0 else "episode",
                "pinned": index < 2,
            },
            headers=auth(token),
        )


@pytest.mark.usefixtures("furnished")
class TestFilters:
    async def test_by_pinned(self, client: AsyncClient, token: str) -> None:
        response = await client.get(
            "/v1/personas/work/fields?pinned=true&limit=100", headers=auth(token)
        )

        assert len(response.json()["fields"]) == 2

    async def test_by_source(self, client: AsyncClient, token: str) -> None:
        response = await client.get(
            "/v1/personas/work/fields?source=owner&limit=100", headers=auth(token)
        )

        assert {f["source"] for f in response.json()["fields"]} == {"owner"}

    async def test_by_key_prefix(self, client: AsyncClient, token: str) -> None:
        response = await client.get(
            "/v1/personas/work/fields?key_prefix=key0&limit=100", headers=auth(token)
        )

        assert len(response.json()["fields"]) == 10

    async def test_a_batch_of_named_keys_in_one_call(self, client: AsyncClient, token: str) -> None:
        # So an assistant makes one call rather than thirty.
        response = await client.get(
            "/v1/personas/work/fields?keys=key00,key05,Key11", headers=auth(token)
        )

        assert sorted(f["key"] for f in response.json()["fields"]) == [
            "key00",
            "key05",
            "key11",
        ]

    async def test_by_note_kind(self, client: AsyncClient, token: str) -> None:
        response = await client.get(
            "/v1/personas/work/notes?kind=lesson&limit=100", headers=auth(token)
        )

        assert {n["kind"] for n in response.json()["notes"]} == {"lesson"}

    async def test_filters_combine(self, client: AsyncClient, token: str) -> None:
        response = await client.get(
            "/v1/personas/work/notes?kind=lesson&pinned=true&limit=100", headers=auth(token)
        )

        notes = response.json()["notes"]
        assert all(n["kind"] == "lesson" and n["pinned"] for n in notes)

    async def test_forgotten_rows_are_out_by_default_and_in_when_asked(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.delete("/v1/personas/work/fields/key00", headers=auth(token))

        default = await client.get("/v1/personas/work/fields?limit=100", headers=auth(token))
        everything = await client.get(
            "/v1/personas/work/fields?limit=100&include_forgotten=true", headers=auth(token)
        )

        assert len(default.json()["fields"]) == 11
        assert len(everything.json()["fields"]) == 12


@pytest.mark.usefixtures("furnished")
class TestSearchingFromAListEndpoint:
    """`?q=` on a list, which is ranked rather than ordered.

    Search results come back without a cursor on purpose: a cursor over bm25 would mean
    re-ranking the whole corpus for every page, and the answer could change underneath
    the walk. If you want to page, filter; if you want relevance, search.
    """

    async def test_fields_can_be_searched_from_their_list_endpoint(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/fields?q=number", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["fields"]

    async def test_notes_can_be_searched_from_their_list_endpoint(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/notes?q=number", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["notes"]

    async def test_a_search_returns_no_cursor(self, client: AsyncClient, token: str) -> None:
        response = await client.get("/v1/personas/work/notes?q=number&limit=2", headers=auth(token))

        assert response.json()["next_cursor"] is None

    async def test_a_search_that_matches_nothing_is_an_empty_list(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/fields?q=unicorns", headers=auth(token))

        assert response.json()["fields"] == []

    async def test_a_search_with_no_words_in_it_is_a_clean_refusal(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/notes?q=***", headers=auth(token))

        assert response.status_code == 422


@pytest.mark.usefixtures("furnished")
class TestPaging:
    async def test_it_walks_every_row_exactly_once(self, client: AsyncClient, token: str) -> None:
        seen: list[str] = []
        url = "/v1/personas/work/notes?limit=5"
        while True:
            page = (await client.get(url, headers=auth(token))).json()
            seen.extend(note["note_id"] for note in page["notes"])
            if page["next_cursor"] is None:
                break
            url = f"/v1/personas/work/notes?limit=5&cursor={page['next_cursor']}"

        assert len(seen) == 12
        assert len(set(seen)) == 12

    async def test_a_forged_cursor_is_a_clean_refusal(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get(
            "/v1/personas/work/notes?cursor=not-a-cursor", headers=auth(token)
        )

        assert response.status_code == 422

    async def test_the_limit_is_capped(self, client: AsyncClient, token: str) -> None:
        # Responses stay a predictable size in a context window.
        response = await client.get("/v1/personas/work/notes?limit=500", headers=auth(token))

        assert response.status_code == 422


@pytest.mark.usefixtures("furnished")
class TestPageSizesComeFromSettings:
    """The page size a caller gets is the deployment's, not a literal written into a route."""

    @pytest.fixture
    def settings(self, tmp_path: Path) -> Settings:
        return build_settings(tmp_path, recall_default_limit=3, recall_max_limit=5)

    async def test_a_list_without_a_limit_uses_the_configured_default(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/notes", headers=auth(token))

        assert response.status_code == 200
        assert len(response.json()["notes"]) == 3
        assert response.json()["next_cursor"] is not None

    async def test_the_configured_maximum_is_enforced_in_the_usual_shape(
        self, client: AsyncClient, token: str
    ) -> None:
        allowed = await client.get("/v1/personas/work/fields?limit=5", headers=auth(token))
        refused = await client.get("/v1/personas/work/fields?limit=6", headers=auth(token))

        assert allowed.status_code == 200
        assert len(allowed.json()["fields"]) == 5
        assert refused.status_code == 422
        problem = refused.json()
        assert problem["type"].endswith("/validation-failed")
        assert [error["location"] for error in problem["errors"]] == ["query.limit"]


@pytest.mark.usefixtures("furnished")
class TestRecall:
    async def test_it_returns_two_sections(self, client: AsyncClient, token: str) -> None:
        response = await client.get("/v1/personas/work/recall?q=number", headers=auth(token))

        body = response.json()
        assert body["fields"]
        assert body["notes"]

    async def test_it_can_span_every_persona(self, client: AsyncClient, token: str) -> None:
        await client.post(
            "/v1/personas/home/notes",
            json={"body": "a note about the dog"},
            headers=auth(token),
        )

        found = (await client.get("/v1/recall?q=dog", headers=auth(token))).json()

        assert [note["profile"] for note in found["notes"]] == ["home"]

    async def test_stemming_finds_a_different_ending(self, client: AsyncClient, token: str) -> None:
        await client.post(
            "/v1/personas/work/notes",
            json={"body": "they prefer concise answers"},
            headers=auth(token),
        )

        found = (await client.get("/v1/recall?q=preferring", headers=auth(token))).json()

        assert found["notes"]

    async def test_a_forgotten_note_stops_coming_back(
        self, client: AsyncClient, token: str
    ) -> None:
        # A tombstone that left the row in the index would be cosmetic.
        written = await client.post(
            "/v1/personas/work/notes",
            json={"body": "a memorable phrase about penguins"},
            headers=auth(token),
        )
        note_id = written.json()["note_id"]
        assert (await client.get("/v1/recall?q=penguins", headers=auth(token))).json()["notes"]

        await client.delete(f"/v1/personas/work/notes/{note_id}", headers=auth(token))

        after = (await client.get("/v1/recall?q=penguins", headers=auth(token))).json()
        assert after["notes"] == []
        # And it is still there when explicitly asked for, because forgetting is soft.
        restored = await client.get(
            f"/v1/personas/work/notes/{note_id}?include_forgotten=true", headers=auth(token)
        )
        assert restored.status_code == 200


class TestHostileSearchStrings:
    @pytest.mark.parametrize("query", HOSTILE)
    async def test_nothing_is_ever_a_five_hundred(
        self, client: AsyncClient, token: str, query: str
    ) -> None:
        # Before the sanitiser, eight of ten of these raised OperationalError out of
        # FTS5 -- on the endpoint a model is most likely to call with whatever somebody
        # just said.
        await client.post(
            "/v1/personas/work/notes", json={"body": "something to find"}, headers=auth(token)
        )

        response = await client.get("/v1/recall", params={"q": query}, headers=auth(token))

        assert response.status_code in {200, 422}, f"{query!r} -> {response.status_code}"

    @pytest.mark.parametrize("query", HOSTILE)
    async def test_the_same_holds_for_a_scoped_recall(
        self, client: AsyncClient, token: str, query: str
    ) -> None:
        await client.post(
            "/v1/personas/work/notes", json={"body": "something to find"}, headers=auth(token)
        )

        response = await client.get(
            "/v1/personas/work/recall", params={"q": query}, headers=auth(token)
        )

        assert response.status_code in {200, 422}, f"{query!r} -> {response.status_code}"


@pytest.mark.usefixtures("furnished")
class TestExport:
    async def test_it_returns_the_whole_persona_in_one_call(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/v1/personas/work/export?limit=100", headers=auth(token))

        body = response.json()
        assert body["persona"]["profile"] == "work"
        assert len(body["fields"]["fields"]) == 12
        assert len(body["notes"]["notes"]) == 12

    async def test_the_two_halves_page_independently(self, client: AsyncClient, token: str) -> None:
        response = await client.get("/v1/personas/work/export?limit=5", headers=auth(token))

        body = response.json()
        assert body["fields"]["next_cursor"] is not None
        assert body["notes"]["next_cursor"] is not None
        assert body["fields"]["next_cursor"] != body["notes"]["next_cursor"]


@pytest.mark.usefixtures("furnished")
class TestTheEventLog:
    async def test_it_records_what_happened(self, client: AsyncClient, token: str) -> None:
        response = await client.get("/v1/personas/work/events?limit=100", headers=auth(token))

        actions = [event["action"] for event in response.json()["events"]]
        assert "note.written" in actions
        assert "field.set" in actions
        assert "persona.created" in actions

    async def test_it_never_carries_a_value_or_a_body(
        self, client: AsyncClient, token: str
    ) -> None:
        await client.put(
            "/v1/personas/work/fields/secretive",
            json={"description": "a description", "value": "a distinctive value"},
            headers=auth(token),
        )
        await client.post(
            "/v1/personas/work/notes",
            json={"body": "another distinctive phrase"},
            headers=auth(token),
        )

        response = await client.get("/v1/personas/work/events?limit=100", headers=auth(token))

        # It is the one table here with no tombstone, so anything written into it is
        # permanent -- which is why it holds the key and the id and nothing else.
        assert "distinctive" not in response.text
