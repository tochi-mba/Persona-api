"""Believing a keyring token, and the six ways of refusing one.

This mirrors ``Keyring-api/src/keyring_api/accounts/signing.py`` lines 113-135
deliberately and almost line for line. The two services have to agree about what a valid
token is, and the cheapest way to keep them agreeing is for the verifying half to be a
recognisable copy of the minting half rather than an independent re-derivation of it.

## The algorithm list is fixed, and is never read from the token

``algorithms=["RS256"]``. Leaving it open is the classic JWT failure and it comes in two
flavours, both of which have a test here:

* ``alg: none`` -- a token with no signature at all, which a library that trusts the
  header will happily accept as unsigned-and-therefore-fine.
* HS256 signed with the **public** key. The public key is published at a URL designed to
  be fetched by anybody, so if the verifier will accept a symmetric algorithm then the
  verifying key is also a forging key.

## Expiry is checked against the injected clock, not PyJWT's

``verify_exp`` is turned off and the check is done here. That is the codebase invariant
-- nothing reads the wall clock directly -- and it is what makes the rule testable at
all: with PyJWT doing it, a test could only ever assert that a token minted now is valid
now. The comparison is ``>=``, so a token is refused *at* its expiry rather than during
the second it names.

## Every refusal is the same refusal

Bad signature, wrong audience, wrong issuer, expired, missing claim, malformed, no
``kid``: one :class:`~persona_api.domain.errors.AuthenticationError`, one message, one
response body. A caller holding a forged token learns nothing from the difference, and
the specific reason goes to the log where only an operator reads it.

This also keeps PyJWT's exception types out of the layers above -- which an import-linter
contract forbids, and which would otherwise make swapping the JWT library a change to
the HTTP handlers.

keyring being *unreachable* is deliberately not one of these. See :mod:`.jwks`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt

from persona_api.core.logging import get_logger
from persona_api.domain.errors import AuthenticationError

if TYPE_CHECKING:
    from persona_api.auth.jwks import JwksClient
    from persona_api.core.clock import Clock

logger = get_logger(__name__)

ALGORITHM = "RS256"

REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub", "aud"]
"""Exactly what keyring puts in a token. No email, no roles, no profile list."""

BAD_TOKEN = "the token was not accepted"  # noqa: S105 -- a message, not a credential
"""One message for every verification failure. Which one it was is nobody's business."""


@dataclass(frozen=True, slots=True)
class VerifiedCaller:
    """Who a verified token says is asking.

    Two fields, and the whole of what this service knows about a caller. There is no
    email here, no role and no permission set, because keyring's token carries none --
    and because there is nothing in this service that a role could grant. See
    ``docs/adr/0003-no-administrative-surface.md``.
    """

    account_id: str
    """The token's ``sub``: the opaque keyring account id. The only identity we get."""

    audience: str
    """The token's ``aud``.

    This becomes ``asserted_by`` on every write the caller makes. It is server-derived
    -- read from the verified claims, never from a request body -- which is exactly what
    makes it the trustworthy half of a field's provenance.
    """


class TokenVerifier:
    """Turns a bearer token into a :class:`VerifiedCaller`, or refuses it."""

    def __init__(self, *, jwks: JwksClient, issuer: str, audience: str, clock: Clock) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience
        self._clock = clock

    async def verify(self, token: str) -> VerifiedCaller:
        """Check a token keyring minted and say who it is for.

        Raises:
            AuthenticationError: expired, wrong audience, wrong issuer, unsigned,
                tampered with, or missing a required claim -- undifferentiated.
            KeyringUnreachableError: the keys could not be fetched. Not the caller's
                fault and deliberately not their error.
        """
        kid = self._key_id(token)
        key = await self._jwks.key_for(kid)

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": REQUIRED_CLAIMS,
                    # Checked below against the injected clock instead. PyJWT would read
                    # the wall clock, which breaks the codebase invariant and makes the
                    # expiry rule untestable without waiting.
                    "verify_exp": False,
                },
            )
        except jwt.InvalidTokenError as exc:
            logger.info("token_rejected", error_type=type(exc).__name__)
            raise AuthenticationError(BAD_TOKEN) from exc

        if self._clock.now().timestamp() >= float(claims["exp"]):
            logger.info("token_rejected", error_type="ExpiredSignatureError")
            raise AuthenticationError(BAD_TOKEN)

        return VerifiedCaller(account_id=str(claims["sub"]), audience=str(claims["aud"]))

    def _key_id(self, token: str) -> str:
        """Read the ``kid`` out of the unverified header.

        Unverified because it has to be: the header names the key, so it is read before
        there is anything to verify with. Nothing else from this header is used, and the
        ``alg`` in it is ignored entirely -- see the module docstring.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            logger.info("token_rejected", error_type=type(exc).__name__)
            raise AuthenticationError(BAD_TOKEN) from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            # keyring always sets one. A token without it was not minted by keyring,
            # and guessing which key to try would be doing an attacker's search for it.
            logger.info("token_rejected", error_type="MissingKeyId")
            raise AuthenticationError(BAD_TOKEN)
        return kid
