"""Every way of forging a keyring token, and the one answer they all get.

This is the highest-value group in the project. persona-api's only identity check is
"does this token verify", so every class here is named for a specific way that check
could be wrong -- and two of them (``TestAlgorithmConfusion``, ``TestExpiry``) are for
mistakes that a verifier can make while appearing to work perfectly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from persona_api.auth.jwks import JwksClient
from persona_api.auth.verifier import BAD_TOKEN, REQUIRED_CLAIMS, TokenVerifier
from persona_api.domain.errors import AuthenticationError, KeyringUnreachableError
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import AUDIENCE, ISSUER, FakeJwksEndpoint, FakeKeyring, forge

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

JWKS_URL = "https://keyring.test/.well-known/jwks.json"

CLAIMS = {
    "iss": ISSUER,
    "sub": "acct_attacker",
    "aud": AUDIENCE,
    "iat": int(EPOCH.timestamp()),
    "exp": int(EPOCH.timestamp()) + 900,
}


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
async def verifier(endpoint: FakeJwksEndpoint, clock: FakeClock) -> AsyncIterator[TokenVerifier]:
    client = JwksClient(
        url=JWKS_URL,
        clock=clock,
        cache_seconds=3600.0,
        min_refetch_seconds=60.0,
        timeout_seconds=5.0,
        transport=endpoint.transport(),
    )
    try:
        yield TokenVerifier(jwks=client, issuer=ISSUER, audience=AUDIENCE, clock=clock)
    finally:
        await client.aclose()


class TestTheHappyPath:
    async def test_a_token_keyring_minted_verifies(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        caller = await verifier.verify(keyring.mint(issued_at=EPOCH, subject="acct_one"))

        assert caller.account_id == "acct_one"

    async def test_the_audience_comes_back_because_it_becomes_asserted_by(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # This is the server-derived half of a field's provenance. It is read from the
        # verified claims and can therefore never be set by a request body -- which is
        # the entire reason it is worth recording.
        caller = await verifier.verify(keyring.mint(issued_at=EPOCH))

        assert caller.audience == AUDIENCE


class TestAudience:
    async def test_a_token_minted_for_another_service_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # Perfectly valid over at media-tool, and that is the point: the `aud` claim is
        # the whole reason keyring mints a token per service rather than one token that
        # opens everything.
        forged = keyring.mint(issued_at=EPOCH, audience="media-tool")

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)


class TestIssuer:
    async def test_a_token_from_a_keyring_we_do_not_trust_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        forged = keyring.mint(issued_at=EPOCH, issuer="https://keyring.evil")

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)


class TestExpiry:
    async def test_a_token_is_valid_up_to_the_instant_it_expires(
        self, verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        token = keyring.mint(issued_at=EPOCH, lifetime_seconds=900)
        clock.advance(899)

        # Asserting the subject rather than truthiness: `assert await verify(...)` is
        # always true for any object, so it would pass against a verifier that returned
        # a caller for a token it should have refused.
        assert (await verifier.verify(token)).account_id == "acct_one"

    async def test_a_token_is_refused_at_its_expiry_not_after_it(
        self, verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # The comparison is >=, so the instant named by `exp` is already too late.
        # Tested through the injected clock rather than a sleep -- with PyJWT doing the
        # check against the wall clock, this test could only ever assert that a token
        # minted now is valid now.
        token = keyring.mint(issued_at=EPOCH, lifetime_seconds=900)
        clock.advance(900)

        with pytest.raises(AuthenticationError):
            await verifier.verify(token)

    async def test_a_long_expired_token_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        token = keyring.mint(issued_at=EPOCH, lifetime_seconds=900)
        clock.advance(86_400)

        with pytest.raises(AuthenticationError):
            await verifier.verify(token)


class TestAlgorithmConfusion:
    """The classic JWT failure, in both of its flavours."""

    async def test_a_token_signed_hs256_with_the_public_key_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # The public key is published at a URL designed to be fetched by anybody. If
        # the verifier would accept a symmetric algorithm, the verifying key is also a
        # forging key and the whole scheme is decorative.
        forged = forge(
            {"alg": "HS256", "typ": "JWT", "kid": keyring.kid},
            CLAIMS,
            secret=keyring.public_pem,
        )

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)

    async def test_a_token_with_no_signature_at_all_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        forged = forge({"alg": "none", "typ": "JWT", "kid": keyring.kid}, CLAIMS, secret=None)

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)


class TestTampering:
    async def test_a_flipped_byte_in_the_payload_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        header, payload, signature = keyring.mint(issued_at=EPOCH).split(".")
        tampered = f"{header}.{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{signature}"

        with pytest.raises(AuthenticationError):
            await verifier.verify(tampered)

    async def test_a_flipped_byte_in_the_signature_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # A character from the middle rather than the end. The final base64 character
        # of a segment carries unused bits, so flipping it can decode to the very same
        # signature bytes -- a test written that way passes or fails by luck.
        header, payload, signature = keyring.mint(issued_at=EPOCH).split(".")
        middle = len(signature) // 2
        swapped = "A" if signature[middle] != "A" else "B"
        tampered = f"{header}.{payload}.{signature[:middle]}{swapped}{signature[middle + 1 :]}"

        with pytest.raises(AuthenticationError):
            await verifier.verify(tampered)


class TestMissingClaims:
    @pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
    async def test_a_token_missing_any_required_claim_is_refused(
        self, verifier: TokenVerifier, keyring: FakeKeyring, claim: str
    ) -> None:
        # Parametrized over the list the verifier actually requires, so adding a claim
        # to that list without minting it correctly fails here rather than in
        # production.
        forged = keyring.mint(issued_at=EPOCH, drop=[claim])

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)


class TestMalformedTokens:
    @pytest.mark.parametrize(
        "token",
        ["", "not-a-token", "a.b", "a.b.c.d", "....", "eyJhbGciOiJSUzI1NiJ9"],
    )
    async def test_something_that_is_not_a_token_is_refused(
        self, verifier: TokenVerifier, token: str
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(token)

    async def test_a_token_with_no_key_id_is_refused(self, verifier: TokenVerifier) -> None:
        # keyring always sets one. A token without it was not minted by keyring, and
        # guessing which key to try would be doing an attacker's search for them.
        forged = forge({"alg": "RS256", "typ": "JWT"}, CLAIMS, secret=b"anything")

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)

    async def test_a_token_whose_key_id_is_not_a_string_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        forged = forge({"alg": "RS256", "typ": "JWT", "kid": 12}, CLAIMS, secret=b"anything")

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)

    async def test_a_token_whose_key_id_is_an_empty_string_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        forged = forge({"alg": "RS256", "typ": "JWT", "kid": ""}, CLAIMS, secret=b"anything")

        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)


class TestOneUndifferentiatedRefusal:
    async def test_every_rejection_carries_the_byte_identical_message(
        self, verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A caller holding a forged token must learn nothing from *which* check failed.
        # Collected into a set rather than asserted one at a time, because the property
        # is that there is exactly one message here.
        expired = keyring.mint(issued_at=EPOCH, lifetime_seconds=1)
        clock.advance(2)
        forgeries = [
            keyring.mint(issued_at=EPOCH, audience="media-tool"),
            keyring.mint(issued_at=EPOCH, issuer="https://keyring.evil"),
            keyring.mint(issued_at=EPOCH, drop=["sub"]),
            expired,
            "not-a-token",
        ]

        messages = set()
        for forged in forgeries:
            with pytest.raises(AuthenticationError) as refusal:
                await verifier.verify(forged)
            messages.add(str(refusal.value))

        assert messages == {BAD_TOKEN}


class TestAnUnknownKeyIsNotARejection:
    async def test_a_kid_nobody_published_reports_keyring_rather_than_the_caller(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # Deliberately not an AuthenticationError. We cannot say whether this token is
        # good, and telling the caller it was rejected sends them to re-authenticate
        # over something that is not their fault.
        forged = keyring.mint(issued_at=EPOCH, kid="a-key-nobody-ever-published")

        with pytest.raises(KeyringUnreachableError):
            await verifier.verify(forged)
