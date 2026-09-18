"""Authentication over HTTP: every refusal identical, and keyring being down is a 503.

Whether a token is good is decided by the family's shared verifier, in ``keyring_client``,
and every one of its rules is tested in the keyring repository against keyring's own
signer. What is pinned here is what this service does with the verdict: the status a caller
sees, that every refusal carries the same body, and what ``/healthy`` says about keyring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from persona_api.auth.jwks import KEYS_STALE, KEYS_UNAVAILABLE
from tests.fakes.keyring import DEFAULT_TTL_SECONDS, ROTATED_KEY, forge_hs256, forge_unsigned
from tests.integration.conftest import auth, token_for

if TYPE_CHECKING:
    from httpx import AsyncClient

    from persona_api.core.config import Settings
    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring

OTHER_ISSUER = "https://keyring.evil"
UNPUBLISHED_KID = "a-key-nobody-ever-published"


class TestRefusals:
    async def test_a_valid_token_is_accepted(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"audience": "downstream-tool"}, id="another-services-audience"),
            pytest.param({"audience": "persona.work"}, id="a-compartment-this-service-lacks"),
            pytest.param({"issuer": OTHER_ISSUER}, id="another-issuer"),
            pytest.param({"omit": "sub"}, id="a-missing-claim"),
            pytest.param({"key": ROTATED_KEY}, id="a-key-keyring-does-not-publish"),
        ],
    )
    async def test_a_token_that_is_not_for_us_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring, overrides: dict[str, Any]
    ) -> None:
        forged = token_for(keyring, **overrides)

        assert (await client.get("/v1/personas", headers=auth(forged))).status_code == 401

    async def test_an_expired_token_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # Judged on the clock the app was wired with, so the boundary is exact: refused at
        # the second the token names, not a second after it.
        token = token_for(keyring)
        clock.advance(DEFAULT_TTL_SECONDS)

        assert (await client.get("/v1/personas", headers=auth(token))).status_code == 401

    @pytest.mark.parametrize("forged", [forge_hs256(), forge_unsigned()], ids=["hs256", "none"])
    async def test_algorithm_confusion_is_refused(self, client: AsyncClient, forged: str) -> None:
        # HS256 signed with the JWKS public key, which anybody can fetch, and no signature
        # at all. Both name a key keyring really publishes.
        assert (await client.get("/v1/personas", headers=auth(forged))).status_code == 401

    @pytest.mark.parametrize("token", ["", "garbage", "a.b.c"])
    async def test_something_that_is_not_a_token_is_refused(
        self, client: AsyncClient, token: str
    ) -> None:
        assert (await client.get("/v1/personas", headers=auth(token))).status_code == 401

    async def test_no_header_at_all_is_refused_in_our_problem_shape(
        self, client: AsyncClient
    ) -> None:
        # HTTPBearer left to itself raises a bare 403 with a plain JSON body -- a
        # different status and a different shape from every other failure here, on the
        # single most common mistake a caller can make.
        response = await client.get("/v1/personas")

        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")


class TestAnUnknownKeyId:
    async def test_a_key_id_keyring_did_not_publish_is_a_401_not_a_503(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        """Keyring answered, and its answer is about the token.

        It is tempting to say an unknown key means we cannot tell whether the token is good.
        But the fetch worked: keyring served its key set and no key by that name is in it,
        which is a fact about the token rather than an outage, and a 503 would tell its
        holder to retry something that can never succeed. A fetch that *fails* is still a
        503 -- see :class:`TestKeyringUnreachable`.
        """
        token = token_for(keyring, kid=UNPUBLISHED_KID)

        response = await client.get("/v1/personas", headers=auth(token))

        assert response.status_code == 401
        assert keyring.fetches == 1


class TestEveryRefusalIsIdentical:
    async def test_the_body_is_the_same_apart_from_the_request_id(
        self, client: AsyncClient, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A caller holding a forged token must learn nothing from which check failed --
        # including whether keyring had ever heard of the key it named.
        expired = token_for(keyring, ttl_seconds=1)
        clock.advance(2)
        refusals = [
            token_for(keyring, audience="downstream-tool"),
            token_for(keyring, audience="persona.work"),
            token_for(keyring, issuer=OTHER_ISSUER),
            token_for(keyring, omit="sub"),
            token_for(keyring, kid=UNPUBLISHED_KID),
            token_for(keyring, key=ROTATED_KEY),
            expired,
            "garbage",
            forge_hs256(),
            forge_unsigned(),
        ]

        bodies = set()
        for token in refusals:
            response = await client.get("/v1/personas", headers=auth(token))
            assert response.status_code == 401
            body = response.json()
            body.pop("request_id", None)
            bodies.add(repr(sorted(body.items())))

        assert len(bodies) == 1, bodies


class TestKeyringUnreachable:
    async def test_nothing_is_fetched_until_something_needs_a_key(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # Startup must not require keyring. A persona service that will not start because
        # keyring is down is a persona service that cannot report keyring being down.
        assert keyring.fetches == 0

        await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert keyring.fetches == 1

    async def test_a_request_gets_503_rather_than_401(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # Nothing is wrong with the caller's token, and a 401 would send them to
        # re-authenticate against a service that is not answering.
        keyring.error = httpx.ConnectError("no route to host")

        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["detail"] == KEYS_UNAVAILABLE

    async def test_the_error_never_names_the_host(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        keyring.error = httpx.ConnectError("no route to 10.1.2.3:8001")

        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert "10.1.2.3" not in response.text


class TestHealth:
    async def test_liveness_says_nothing_about_keyring_at_all(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # An orchestrator restarts a container whose liveness check fails, and restarting
        # this process does not fix keyring. So /healthy answers without asking anything.
        keyring.error = httpx.ConnectError("no route to 10.1.2.3:8001")

        response = await client.get("/healthy")

        assert response.status_code == 200
        assert response.json()["status"] == "alive"
        assert keyring.fetches == 0

    async def test_it_asks_keyring_itself_rather_than_waiting_for_a_token(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # A check that only reported what earlier requests happened to find would have
        # nothing to say on a fresh process -- which is when an operator most wants to know.
        response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json()["checks"]["keyring"] == {
            "status": "ok",
            "detail": {"reachable": True, "reason": None, "fix": None},
        }
        assert keyring.fetches == 1

    async def test_keyring_down_with_no_keys_held_is_degraded_rather_than_dead(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # The process is fine and the database is fine. What fails is every authenticated
        # request, with a 503, so the load balancer is told -- in words a stranger may read.
        keyring.error = httpx.ConnectError("no route to 10.1.2.3:8001")

        response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"]["status"] == "ok"
        assert body["checks"]["keyring"]["status"] == "degraded"
        detail = body["checks"]["keyring"]["detail"]
        assert detail["reachable"] is False
        assert detail["reason"] == KEYS_UNAVAILABLE
        assert "PERSONA_KEYRING_JWKS_URL" in detail["fix"]
        assert "10.1.2.3" not in response.text

    async def test_an_outage_survived_on_cached_keys_is_ok_and_says_so(
        self, client: AsyncClient, keyring: FakeKeyring, clock: FakeClock, settings: Settings
    ) -> None:
        # Tokens still verify against the keys a good fetch left behind, so taking this
        # instance out of rotation would turn keyring's outage into this service's. It is
        # still reported, because the cached keys will not be served for ever.
        assert (await client.get("/ready")).status_code == 200
        keyring.error = httpx.ConnectError("down")
        clock.advance(settings.jwks_cache_seconds)

        response = await client.get("/ready")

        assert response.status_code == 200
        check = response.json()["checks"]["keyring"]
        assert check["status"] == "ok"
        assert check["detail"]["reachable"] is False
        assert check["detail"]["reason"] == KEYS_STALE
        assert "PERSONA_KEYRING_JWKS_URL" in check["detail"]["fix"]


class TestAssertedByComesFromTheToken:
    async def test_it_is_the_tokens_audience(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        await client.put(
            "/v1/personas/work/fields/voice",
            json={"description": "how it speaks", "value": "dry"},
            headers=auth(token_for(keyring)),
        )

        field = (
            await client.get("/v1/personas/work/fields/voice", headers=auth(token_for(keyring)))
        ).json()

        assert field["asserted_by"] == "persona"

    async def test_a_body_claiming_otherwise_is_refused_outright(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # Not "ignored" -- refused. extra="forbid" means a caller that tries to set its
        # own provenance is told so, rather than believing it worked.
        response = await client.put(
            "/v1/personas/work/fields/voice",
            json={
                "description": "how it speaks",
                "value": "dry",
                "asserted_by": "somebody-else",
            },
            headers=auth(token_for(keyring)),
        )

        assert response.status_code == 422

    async def test_two_accounts_writing_the_same_key_get_their_own_field(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        for account, value in (("acct_one", "mine"), ("acct_two", "theirs")):
            await client.put(
                "/v1/personas/work/fields/voice",
                json={"description": "how it speaks", "value": value},
                headers=auth(token_for(keyring, account)),
            )

        mine = await client.get(
            "/v1/personas/work/fields/voice", headers=auth(token_for(keyring, "acct_one"))
        )

        assert mine.json()["value"] == "mine"
