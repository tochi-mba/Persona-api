"""Believing a keyring token: this service's audience, its errors, and one refusal for all.

The rules that decide whether a token is good -- the pinned algorithm, the pinned issuer,
every required claim, expiry on the injected clock, tampering, malformed input -- and every
rule about fetching keyring's keys belong to :mod:`keyring_client`. They are tested
exhaustively in the keyring repository, against keyring's own signer, and a second copy of
that suite here would only drift from it. The few kept prove this adapter hands the shared
verifier this service's issuer, clock and keys.

The rest is what only this service decides: that the audience is exactly ``persona``, that
the library's verdicts become this service's own errors, and that every refusal, from every
path, says the same thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from persona_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient
from persona_api.auth.verifier import ALGORITHM, REQUIRED_CLAIMS, TokenVerifier, VerifiedCaller
from persona_api.domain.errors import AuthenticationError, KeyringUnreachableError
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import (
    AUDIENCE,
    DEFAULT_TTL_SECONDS,
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    FakeKeyring,
    forge_hs256,
    forge_unsigned,
    mint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ACCOUNT = "acct_one"
OTHER_ISSUER = "https://keyring.evil"
UNPUBLISHED_KID = "a-key-nobody-ever-published"


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def verifier(keyring: FakeKeyring, clock: FakeClock) -> AsyncIterator[TokenVerifier]:
    jwks = JwksClient(url=JWKS_URL, clock=clock, transport=keyring.transport())
    try:
        yield TokenVerifier(jwks=jwks, issuer=ISSUER, audience=AUDIENCE, clock=clock)
    finally:
        await jwks.aclose()


class TestTheHappyPath:
    async def test_a_token_keyring_minted_says_which_account_and_audience_it_is_for(
        self, verifier: TokenVerifier
    ) -> None:
        # The audience comes back because it becomes `asserted_by`: the server-derived half
        # of a field's provenance, read from the verified claims and therefore never settable
        # by a request body -- which is the entire reason it is worth recording.
        token = mint(account_id=ACCOUNT)
        caller = await verifier.verify(token)

        assert caller == VerifiedCaller(account_id=ACCOUNT, audience=AUDIENCE, token=token)


class TestTheTokenStaysOffTheWire:
    def test_a_caller_does_not_render_its_token(self) -> None:
        # A log line or an error that printed the caller would otherwise print a live
        # credential. settings-api is shown this string; a log aggregator is not.
        caller = VerifiedCaller(account_id=ACCOUNT, audience=AUDIENCE, token="a-live-credential")

        assert "a-live-credential" not in repr(caller)


class TestTheSharedRulesAreWiredIn:
    def test_the_rules_are_the_familys(self) -> None:
        assert ALGORITHM == "RS256"
        assert set(REQUIRED_CLAIMS) == {"exp", "iat", "iss", "sub", "aud"}

    @pytest.mark.parametrize("forged", [forge_unsigned(), forge_hs256()], ids=["none", "hs256"])
    async def test_the_algorithm_confusion_attacks_are_refused(
        self, verifier: TokenVerifier, forged: str
    ) -> None:
        # The classic JWT failure, in both of its flavours. Each forgery names a key keyring
        # really publishes and carries every claim a good token does, so nothing but the
        # pinned algorithm stands between it and an identity.
        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)

    async def test_this_deployments_issuer_is_the_one_pinned(self, verifier: TokenVerifier) -> None:
        # Signed with the right key, for the right audience, well within its lifetime. The
        # only thing wrong with it is who minted it.
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(issuer=OTHER_ISSUER))

    async def test_a_key_keyring_does_not_publish_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(key=ROTATED_KEY))

    async def test_expiry_is_judged_on_this_services_injected_clock(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        # Refused at the second the token names rather than a second after it, and proved by
        # moving the clock this service injected rather than by waiting fifteen minutes.
        token = mint(account_id=ACCOUNT)
        clock.advance(DEFAULT_TTL_SECONDS - 1)
        assert (await verifier.verify(token)).account_id == ACCOUNT

        clock.advance(1)

        with pytest.raises(AuthenticationError):
            await verifier.verify(token)


class TestAudience:
    @pytest.mark.parametrize(
        "audience", ["media-tool", "persona.work", "personas", "persona-api", "user"]
    )
    async def test_a_token_for_anything_but_exactly_persona_is_refused(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        """Exactly, and not a family.

        A token minted for another service is perfectly valid over there, and that is the
        point: the ``aud`` claim is the whole reason keyring mints a token per service rather
        than one token that opens everything. ``persona.work`` is the case worth naming -- an
        audience *family* would accept it, and this service has no compartment for it to mean.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(audience=audience))


class TestAnUnknownKeyId:
    async def test_a_key_id_missing_from_the_keys_keyring_just_served_is_a_refusal(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        """A 401, not keyring being unreachable.

        It is tempting to say an unknown key means we cannot tell whether the token is good.
        But keyring has just answered: it served its key set, and no key by that name is in
        it. That is a fact about the token, and a 503 would tell its holder to retry
        something that can never succeed. The fetch count is what shows keyring answered.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(kid=UNPUBLISHED_KID))

        assert keyring.fetches == 1


class TestKeyringUnreachable:
    async def test_it_is_this_services_unreachable_error_and_names_nothing(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # Not a refusal: the token may be perfectly good, and a 401 would send its holder to
        # re-authenticate against a service that is not answering. And fixed text rather
        # than the exception's, which names a host the caller can do nothing with.
        keyring.error = httpx.ConnectError("no route to 10.1.2.3:8001")

        with pytest.raises(KeyringUnreachableError) as failure:
            await verifier.verify(mint())

        assert str(failure.value) == KEYS_UNAVAILABLE


class TestOneUndifferentiatedRefusal:
    async def test_every_rejection_carries_the_byte_identical_message(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        # A caller holding a forged token must learn nothing from *which* check failed --
        # including whether keyring had ever heard of the key it named. Collected into a set
        # rather than asserted one at a time, because the property is that there is exactly
        # one message.
        refusals = [
            mint(audience="media-tool"),
            mint(audience="persona.work"),
            mint(issuer=OTHER_ISSUER),
            mint(omit="sub"),
            mint(key=ROTATED_KEY),
            mint(kid=UNPUBLISHED_KID),
            forge_hs256(),
            forge_unsigned(),
            "not-a-token",
            "",
        ]

        messages: set[str] = set()
        for token in refusals:
            with pytest.raises(AuthenticationError) as refusal:
                await verifier.verify(token)
            messages.add(str(refusal.value))

        # Last, because it is the only refusal here that needs the clock moved.
        clock.advance(DEFAULT_TTL_SECONDS)
        with pytest.raises(AuthenticationError) as expired:
            await verifier.verify(mint())
        messages.add(str(expired.value))

        assert messages == {BAD_TOKEN}
