"""Fetching, caching and refetching the keys that verify keyring's tokens.

persona-api never calls keyring at request time. It verifies keyring's signed tokens
locally against the public keys keyring publishes, which is exactly what that endpoint
is for -- see ``docs/adr/0007-local-jwks-verification.md``. This module is the whole of
the relationship: one outbound GET, cached.

Three properties are load-bearing and none of them is obvious from the call site.

## Fetched lazily, never at startup

A persona service that will not start because keyring is down is a persona service that
cannot report keyring being down. The first token that needs a key is what provokes the
first fetch; until then this object has never opened a socket. ``/healthy`` reads
:attr:`JwksClient.is_reachable` and reports *degraded* rather than dead, which is the
same shape keyring uses for a sealed vault.

## An unknown kid refetches -- at most once per window

This is the part that is a security control rather than a cache tuning knob.

A ``kid`` this client has never seen is the signal that keyring's key was replaced, so
the honest response is to fetch again. But the ``kid`` comes out of an *unverified*
token header, which anybody can write anything into. Without a floor between refetches,
a stream of tokens carrying random kids is one outbound request per request -- an
amplifier, with this service's network position, pointed at the one service every other
service authenticates against.

So the refetch is rate-limited to one per ``min_refetch_seconds``, and a token whose kid
is unknown *within* that window is refused as unverifiable rather than being allowed to
provoke a fetch. The test asserts the fetch **count**, because "a refetch happens" is not
the property -- "no more than one refetch happens per window, however many unknown kids
arrive" is.

## Keyring being down is not the caller's fault

A failed fetch raises :class:`~persona_api.domain.errors.KeyringUnreachableError`, never
:class:`~persona_api.domain.errors.AuthenticationError`. Nothing is wrong with the
caller's token, and telling them it was rejected sends them to re-authenticate against a
service that is not answering. It becomes a 503 with a problem body, and the last error
is kept for ``/healthy`` to report.

keyring has no key rotation, and its ``kid`` is a thumbprint of the key itself, so it is
stable across restarts. A changed ``kid`` means the key was genuinely replaced.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import jwt

from persona_api.core.logging import get_logger
from persona_api.domain.errors import KeyringUnreachableError

if TYPE_CHECKING:
    from persona_api.core.clock import Clock

logger = get_logger(__name__)

ALGORITHM = "RS256"

UNREACHABLE = "the keys that verify your token could not be fetched; try again shortly"
"""What a caller is told. Names no host, no path and no error text.

The detail goes to the log and to ``/healthy``, where an operator reads it. A caller
being told which URL failed learns the internal topology and can do nothing with it.
"""


class JwksClient:
    """keyring's public keys, by ``kid``, with one socket between us and them."""

    # PLR0913: six keyword-only arguments, because a JWKS client genuinely has six
    # collaborators -- where, what time it is, two windows, a timeout, and a transport
    # the tests substitute. Grouping them into a settings object would add a type whose
    # only job is to be unpacked one line later, and would put configuration inside a
    # module the domain-independence argument keeps free of it.
    def __init__(  # noqa: PLR0913
        self,
        *,
        url: str,
        clock: Clock,
        cache_seconds: float,
        min_refetch_seconds: float,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._min_refetch_seconds = min_refetch_seconds
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        """Why the last fetch failed, for ``/healthy``. ``None`` once one succeeds."""
        return self._last_error

    @property
    def is_reachable(self) -> bool | None:
        """Whether keyring answered last time. ``None`` before the first attempt.

        Three states rather than two, because "we have never asked" is not "it is
        down" -- and reporting a service unreachable before anything has tried to reach
        it would have every fresh deployment start degraded.
        """
        if self._fetched_at is None and self._last_error is None:
            return None
        return self._last_error is None

    async def key_for(self, kid: str) -> Any:
        """Return the key with this id, fetching or refetching if it is not held.

        Raises:
            KeyringUnreachableError: if keyring could not be reached, or if the key set
                it served does not contain this ``kid`` -- the two are the same answer
                to the caller, because in both cases we cannot say whether the token is
                good.
        """
        fetched_at = self._fetched_at
        if fetched_at is None or self._clock.monotonic() - fetched_at >= self._cache_seconds:
            # Carried in a local rather than re-read from the attribute, so the rest of
            # this method has a timestamp that is known to exist. Read back off self it
            # would be `float | None` forever, and the None arm would be a branch no
            # test could ever reach -- which is the coverage gate telling you the code
            # is shaped wrong rather than telling you to write a stranger test.
            fetched_at = await self._fetch()

        key = self._keys.get(kid)
        if key is not None:
            return key

        # An unknown kid means either a replaced key or a forged header, and this is
        # where the difference costs money: refetching for every forgery is the
        # amplifier. The floor is what makes the honest case work and the hostile one
        # free.
        if self._clock.monotonic() - fetched_at >= self._min_refetch_seconds:
            await self._fetch()
            key = self._keys.get(kid)
            if key is not None:
                return key

        logger.info("jwks_unknown_kid", kid=kid)
        raise KeyringUnreachableError(UNREACHABLE)

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _fetch(self) -> float:
        """Replace the held key set and return when the attempt was made.

        The timestamp is recorded on failure too. Without that, a keyring that is down
        would be retried on every single request -- which is the same amplifier as the
        unknown-kid one, pointed at a service that is already struggling.
        """
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
            keys = _parse(document)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self._fetched_at = self._clock.monotonic()
            self._last_error = type(exc).__name__
            logger.warning("jwks_fetch_failed", error_type=type(exc).__name__)
            raise KeyringUnreachableError(UNREACHABLE) from exc

        self._keys = keys
        self._fetched_at = self._clock.monotonic()
        self._last_error = None
        logger.info("jwks_fetched", keys=len(keys))
        return self._fetched_at


def _parse(document: object) -> dict[str, Any]:
    """Turn a JWKS document into usable keys, by ``kid``.

    Only RS256 signing keys are taken. A document that declared some other algorithm
    would be a keyring we do not understand, and silently ignoring the entry is better
    than building a key we would then never be allowed to verify with -- the algorithm
    list in the verifier is fixed and does not consult this.

    Raises:
        ValueError: if the document is not a key set at all. Caught by the caller and
            reported as keyring being unreachable, because a keyring serving something
            that is not a JWKS is unreachable in every sense that matters here.
    """
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        # ValueError rather than the TypeError ruff would prefer: this is a malformed
        # *document*, not a caller passing the wrong type, and the caller catches it
        # alongside every other way a fetch can come back unusable.
        msg = "the JWKS document has no key set in it"
        raise ValueError(msg)  # noqa: TRY004

    keys: dict[str, Any] = {}
    for entry in document["keys"]:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA" or "kid" not in entry:
            continue
        keys[str(entry["kid"])] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(entry))

    if not keys:
        msg = "the JWKS document holds no RSA keys"
        raise ValueError(msg)
    return keys
