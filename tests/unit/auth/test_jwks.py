"""Caching, refetching, and the rate limit that is a security control.

The class worth reading is :class:`TestTheRefetchAmplifier`. Every other property here
is ordinary cache behaviour; that one is the reason this module has a rate limit at all,
and it asserts on the **fetch count** rather than on whether a refetch happened --
because "a refetch happens" is not the property. "However many unknown kids arrive,
keyring is fetched at most once per window" is.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from persona_api.auth.jwks import JwksClient
from persona_api.domain.errors import KeyringUnreachableError
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FakeJwksEndpoint, FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

JWKS_URL = "https://keyring.test/.well-known/jwks.json"
CACHE_SECONDS = 3600.0
MIN_REFETCH_SECONDS = 60.0


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
async def client(endpoint: FakeJwksEndpoint, clock: FakeClock) -> AsyncIterator[JwksClient]:
    jwks = JwksClient(
        url=JWKS_URL,
        clock=clock,
        cache_seconds=CACHE_SECONDS,
        min_refetch_seconds=MIN_REFETCH_SECONDS,
        timeout_seconds=5.0,
        transport=endpoint.transport(),
    )
    try:
        yield jwks
    finally:
        await jwks.aclose()


class TestLazyFetching:
    async def test_constructing_the_client_contacts_nothing(
        self, client: JwksClient, endpoint: FakeJwksEndpoint
    ) -> None:
        # Startup must not require keyring to be reachable. A persona service that will
        # not start because keyring is down is a persona service that cannot report
        # keyring being down.
        assert endpoint.fetches == 0
        assert client.is_reachable is None

    async def test_the_first_key_request_is_what_fetches(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        await client.key_for(keyring.kid)

        assert endpoint.fetches == 1
        assert client.is_reachable is True


class TestCaching:
    async def test_a_second_request_for_a_known_key_does_not_refetch(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        await client.key_for(keyring.kid)
        await client.key_for(keyring.kid)
        await client.key_for(keyring.kid)

        assert endpoint.fetches == 1

    async def test_the_cache_expires_after_its_window(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        clock.advance(CACHE_SECONDS)
        await client.key_for(keyring.kid)

        assert endpoint.fetches == 2

    async def test_the_cache_still_holds_just_before_its_window_closes(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        clock.advance(CACHE_SECONDS - 1)
        await client.key_for(keyring.kid)

        assert endpoint.fetches == 1


class TestTheRefetchAmplifier:
    """The rate limit, which is a security control and not a tuning knob.

    The ``kid`` arrives in an *unverified* token header, so anybody can put anything in
    it. Without a floor between refetches, a stream of tokens carrying random kids is
    one outbound request per inbound request -- an amplifier, with this service's
    network position, pointed at the one service every other service authenticates
    against.
    """

    async def test_a_stream_of_invented_key_ids_provokes_one_fetch_not_a_hundred(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        # Past the floor, so the first unknown kid is genuinely allowed to refetch --
        # otherwise this test would pass for the wrong reason, on a client that never
        # refetched at all.
        clock.advance(MIN_REFETCH_SECONDS)

        for attempt in range(100):
            with pytest.raises(KeyringUnreachableError):
                await client.key_for(f"invented-{attempt}")

        # One refetch for the first unknown kid, then the floor holds for the other
        # ninety-nine. The count is the assertion: a test that only checked "it raised"
        # would pass just as well against a client that fetched a hundred times, which
        # is precisely the bug this rate limit exists to prevent.
        assert endpoint.fetches == 2

    async def test_an_unknown_kid_just_after_a_fetch_does_not_provoke_another(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        # The floor is measured from the last fetch, successful or not -- so a forged
        # kid arriving moments after a good one costs nothing at all. The price is that
        # a genuine key rotation is invisible for up to one window, which is the trade
        # the window exists to make.
        await client.key_for(keyring.kid)

        with pytest.raises(KeyringUnreachableError):
            await client.key_for("invented")

        assert endpoint.fetches == 1

    async def test_the_floor_lifts_once_the_window_has_passed(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        clock.advance(MIN_REFETCH_SECONDS)
        with pytest.raises(KeyringUnreachableError):
            await client.key_for("invented")
        assert endpoint.fetches == 2

        clock.advance(MIN_REFETCH_SECONDS)
        with pytest.raises(KeyringUnreachableError):
            await client.key_for("invented")

        assert endpoint.fetches == 3

    async def test_an_unknown_kid_inside_the_window_never_reaches_keyring(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        clock.advance(MIN_REFETCH_SECONDS)
        with pytest.raises(KeyringUnreachableError):
            await client.key_for("invented")
        fetches_after_first = endpoint.fetches

        clock.advance(MIN_REFETCH_SECONDS - 1)
        with pytest.raises(KeyringUnreachableError):
            await client.key_for("another-invented")

        assert endpoint.fetches == fetches_after_first


class TestKeyRotation:
    async def test_a_replaced_key_is_picked_up_on_the_next_refetch(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # keyring has no rotation and its kid is a thumbprint of the key, so a changed
        # kid means the key was genuinely replaced -- an operator action, not an
        # attack. It has to be picked up without a restart.
        await client.key_for(keyring.kid)
        replacement = FakeKeyring()
        endpoint.serve(replacement)
        clock.advance(MIN_REFETCH_SECONDS)

        assert await client.key_for(replacement.kid) is not None

    async def test_the_old_key_stops_working_once_it_is_gone(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        await client.key_for(keyring.kid)
        endpoint.serve(FakeKeyring())
        clock.advance(CACHE_SECONDS)

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)


class TestKeyringUnreachable:
    async def test_a_connection_failure_is_not_the_callers_fault(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        # KeyringUnreachableError, never AuthenticationError. Nothing is wrong with the
        # caller's token, and a 401 would send them to re-authenticate against a
        # service that is not answering.
        endpoint.fail_with = httpx.ConnectError("no route to host")

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)

    @pytest.mark.parametrize("status_code", [404, 500, 503])
    async def test_a_bad_status_is_reported_as_unreachable(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, status_code: int
    ) -> None:
        endpoint.status_code = status_code

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)

    @pytest.mark.parametrize(
        "body",
        [
            "not json at all",
            "[]",
            '{"keys": "not a list"}',
            '{"keys": []}',
            # Valid JSON, valid key set shape, but nothing in it we can verify with.
            '{"keys": [{"kty": "EC", "kid": "x"}]}',
            # An RSA entry with no kid cannot be looked up by one.
            '{"keys": [{"kty": "RSA", "n": "AQAB", "e": "AQAB"}]}',
        ],
    )
    async def test_a_body_that_is_not_a_key_set_is_reported_as_unreachable(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, body: str
    ) -> None:
        # A keyring serving something that is not a JWKS is unreachable in every sense
        # that matters here: we cannot verify anything, and no caller can fix it.
        endpoint.body = body

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)

    async def test_the_failure_is_recorded_for_healthy_to_report(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        endpoint.fail_with = httpx.ConnectError("no route to host")

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)

        assert client.is_reachable is False
        assert client.last_error == "ConnectError"

    async def test_the_error_text_never_reaches_the_caller(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        # It can carry a hostname or an internal path, and the caller can do nothing
        # with either. The detail goes to the log and to /healthy, where an operator
        # reads it.
        endpoint.fail_with = httpx.ConnectError("no route to 10.1.2.3:8001")

        with pytest.raises(KeyringUnreachableError) as failure:
            await client.key_for(keyring.kid)

        assert "10.1.2.3" not in str(failure.value)

    async def test_a_failing_keyring_is_not_retried_on_every_request(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        # The same amplifier as the unknown-kid one, pointed at a service that is
        # already struggling. The failed attempt records its timestamp, so the floor
        # applies to failures too.
        endpoint.fail_with = httpx.ConnectError("down")
        for _ in range(20):
            with pytest.raises(KeyringUnreachableError):
                await client.key_for(keyring.kid)

        assert endpoint.fetches == 1

    async def test_it_recovers_once_keyring_answers_again(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        endpoint.fail_with = httpx.ConnectError("down")
        with pytest.raises(KeyringUnreachableError):
            await client.key_for(keyring.kid)

        endpoint.fail_with = None
        clock.advance(CACHE_SECONDS)

        assert await client.key_for(keyring.kid) is not None
        assert client.is_reachable is True
        assert client.last_error is None


class TestTheKeySet:
    async def test_entries_that_are_not_rsa_signing_keys_are_skipped(
        self, client: JwksClient, endpoint: FakeJwksEndpoint, keyring: FakeKeyring
    ) -> None:
        # A document declaring some other algorithm is a keyring we do not understand.
        # Skipping the entry beats building a key the fixed algorithm list would then
        # never let us verify with.
        document = keyring.jwks()
        document["keys"] = [
            {"kty": "EC", "kid": "elliptic"},
            "not even an object",
            *document["keys"],
        ]
        endpoint.body = json.dumps(document)

        assert await client.key_for(keyring.kid) is not None
