"""Authentication over HTTP: every refusal identical, and keyring being down is a 503."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from persona_api.auth.verifier import REQUIRED_CLAIMS
from tests.fakes.clock import EPOCH
from tests.fakes.keyring import forge
from tests.integration.conftest import auth, token_for

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeJwksEndpoint, FakeKeyring

CLAIMS = {
    "iss": "https://keyring.test",
    "sub": "acct_attacker",
    "aud": "persona",
    "iat": int(EPOCH.timestamp()),
    "exp": int(EPOCH.timestamp()) + 900,
}


class TestRefusals:
    async def test_a_valid_token_is_accepted(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("name", "overrides"),
        [
            ("another service's audience", {"audience": "media-tool"}),
            ("another issuer", {"issuer": "https://keyring.evil"}),
            ("a missing claim", {"drop": ["sub"]}),
        ],
    )
    async def test_a_token_that_is_not_for_us_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring, name: str, overrides: dict[str, object]
    ) -> None:
        assert name
        forged = keyring.mint(issued_at=EPOCH, **overrides)  # type: ignore[arg-type]

        response = await client.get("/v1/personas", headers=auth(forged))

        assert response.status_code == 401

    @pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
    async def test_each_required_claim_is_required(
        self, client: AsyncClient, keyring: FakeKeyring, claim: str
    ) -> None:
        forged = keyring.mint(issued_at=EPOCH, drop=[claim])

        assert (await client.get("/v1/personas", headers=auth(forged))).status_code == 401

    async def test_an_expired_token_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        token = token_for(keyring, lifetime_seconds=900)
        clock.advance(900)

        assert (await client.get("/v1/personas", headers=auth(token))).status_code == 401

    async def test_algorithm_confusion_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # HS256 signed with the JWKS public key, which anybody can fetch.
        forged = forge(
            {"alg": "HS256", "typ": "JWT", "kid": keyring.kid},
            CLAIMS,
            secret=keyring.public_pem,
        )

        assert (await client.get("/v1/personas", headers=auth(forged))).status_code == 401

    async def test_an_unsigned_token_is_refused(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        forged = forge({"alg": "none", "typ": "JWT", "kid": keyring.kid}, CLAIMS, secret=None)

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


class TestEveryRefusalIsIdentical:
    async def test_the_body_is_the_same_apart_from_the_request_id(
        self, client: AsyncClient, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A caller holding a forged token must learn nothing from which check failed.
        expired = token_for(keyring, lifetime_seconds=1)
        clock.advance(2)
        forgeries = [
            keyring.mint(issued_at=EPOCH, audience="media-tool"),
            keyring.mint(issued_at=EPOCH, issuer="https://keyring.evil"),
            keyring.mint(issued_at=EPOCH, drop=["sub"]),
            expired,
            "garbage",
            forge({"alg": "none", "typ": "JWT", "kid": keyring.kid}, CLAIMS, secret=None),
        ]

        bodies = set()
        for forged in forgeries:
            response = await client.get("/v1/personas", headers=auth(forged))
            body = response.json()
            body.pop("request_id", None)
            bodies.add(repr(sorted(body.items())))

        assert len(bodies) == 1, bodies


class TestKeyringUnreachable:
    async def test_the_app_starts_without_keyring(
        self, client: AsyncClient, endpoint: FakeJwksEndpoint
    ) -> None:
        # Startup must not require keyring. A persona service that will not start
        # because keyring is down is a persona service that cannot report keyring being
        # down.
        endpoint.fail_with = httpx.ConnectError("no route to host")

        assert (await client.get("/healthy")).status_code == 200

    async def test_a_request_gets_503_rather_than_401(
        self, client: AsyncClient, keyring: FakeKeyring, endpoint: FakeJwksEndpoint
    ) -> None:
        # Nothing is wrong with the caller's token, and a 401 would send them to
        # re-authenticate against a service that is not answering.
        endpoint.fail_with = httpx.ConnectError("no route to host")

        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_the_error_never_names_the_host(
        self, client: AsyncClient, keyring: FakeKeyring, endpoint: FakeJwksEndpoint
    ) -> None:
        endpoint.fail_with = httpx.ConnectError("no route to 10.1.2.3:8001")

        response = await client.get("/v1/personas", headers=auth(token_for(keyring)))

        assert "10.1.2.3" not in response.text

    async def test_health_reports_it_as_degraded_rather_than_dead(
        self, client: AsyncClient, keyring: FakeKeyring, endpoint: FakeJwksEndpoint
    ) -> None:
        endpoint.fail_with = httpx.ConnectError("down")
        await client.get("/v1/personas", headers=auth(token_for(keyring)))

        response = await client.get("/healthy")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["keyring"]["detail"]["reachable"] is False
        assert body["checks"]["database"]["status"] == "ok"
        assert "PERSONA_KEYRING_JWKS_URL" in body["checks"]["keyring"]["detail"]["fix"]

    async def test_health_is_ok_before_anything_has_needed_a_key(self, client: AsyncClient) -> None:
        # "Never asked" is not "down". Reporting it as down would have every fresh
        # deployment start degraded before anybody had called it.
        response = await client.get("/healthy")

        assert response.status_code == 200
        assert response.json()["checks"]["keyring"]["detail"]["reachable"] is None


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
