"""Believing a keyring token, and the one way of refusing one.

Every rule about the token itself belongs to :class:`keyring_client.TokenVerifier`, which
every service in the family shares and which is tested against keyring's own signer in the
keyring repository: the algorithm pinned to RS256 and never read from the header, the
issuer pinned, every claim keyring sets required, expiry checked against the injected clock
rather than PyJWT's wall clock, and one refusal for all of it. The verifying half has to
agree with the minting half about what a valid token is, and one shared copy of the rules is
the cheapest way to keep six services agreeing with keyring rather than with each other.

What is left here is what only this service decides.

## The audience is exactly ``persona``

:class:`~keyring_client.ExactAudience`, not a family. persona-api has no scopes and no
compartments -- a token is either for this service or it is not -- so ``persona.work`` is
refused exactly as ``example-tool`` is. The name comes from ``PERSONA_AUDIENCE``.

## The vocabulary is this service's own

The library's errors become domain errors at this boundary, so nothing above ``auth``
knows a library was involved, and an import-linter contract keeps it that way.

* Every refusal is :class:`~persona_api.domain.errors.AuthenticationError` carrying
  :data:`BAD_TOKEN`, whichever rule refused -- bad signature, wrong audience, wrong issuer,
  expired, missing claim, malformed, or a key id keyring does not publish. A caller holding
  a forged token learns nothing from the difference, and the specific reason goes to the
  log where only an operator reads it.
* Keyring being unreachable is :class:`~persona_api.domain.errors.KeyringUnreachableError`
  carrying :data:`KEYS_UNAVAILABLE`, and becomes a 503 rather than a 401. Nothing is wrong
  with the caller's token, and telling them it was rejected sends them to re-authenticate
  against a service that is not answering. Fixed text, never the exception's own message:
  that carries the URL, and a URL can carry credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keyring_client import ALGORITHM, REQUIRED_CLAIMS, ExactAudience
from keyring_client import AuthenticationError as TokenRefusedError
from keyring_client import KeyringUnreachableError as KeysUnavailableError
from keyring_client import TokenVerifier as KeyringTokenVerifier

from persona_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE
from persona_api.core.logging import get_logger
from persona_api.domain.errors import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from persona_api.auth.jwks import JwksClient
    from persona_api.core.clock import Clock

__all__ = ["ALGORITHM", "REQUIRED_CLAIMS", "TokenVerifier", "VerifiedCaller"]

logger = get_logger(__name__)


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

    token: str = field(repr=False)
    """The bearer string, presented to settings-api as the user token.

    Held so a request can ask for this person's settings without reading the
    Authorization header a second time. ``repr=False`` because a log line or an error
    that rendered the caller would otherwise print a live credential.
    """


class TokenVerifier:
    """Turns a bearer token into a :class:`VerifiedCaller`, or refuses it."""

    def __init__(self, *, jwks: JwksClient, issuer: str, audience: str, clock: Clock) -> None:
        # This service's logger rather than the library's standard-library fallback, so a
        # refusal's reason lands in the structured, redacted log with the request id on it.
        self._verifier = KeyringTokenVerifier(jwks=jwks, issuer=issuer, clock=clock, logger=logger)
        # Built here rather than per request, so an audience that could never match -- empty,
        # untrimmed, or containing the compartment separator -- stops the service starting
        # instead of quietly refusing every token it is ever sent.
        self._audience = ExactAudience(audience)

    async def verify(self, token: str) -> VerifiedCaller:
        """Check a token keyring minted and say who it is for.

        Raises:
            AuthenticationError: expired, wrong audience, wrong issuer, unsigned, tampered
                with, missing a required claim, or naming a key keyring does not publish --
                undifferentiated.
            KeyringUnreachableError: the keys could not be fetched and no usable copy is
                held. Not the caller's fault and deliberately not their error.
        """
        try:
            verified = await self._verifier.verify(token, audience=self._audience)
        except TokenRefusedError as exc:
            raise AuthenticationError(BAD_TOKEN) from exc
        except KeysUnavailableError as exc:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc

        return VerifiedCaller(
            account_id=verified.account_id, audience=verified.audience, token=token
        )
