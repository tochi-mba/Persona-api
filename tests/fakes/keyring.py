"""A keyring that exists entirely inside the test process.

Real RS256 signing with a locally generated key, a JWKS document in keyring's exact
shape, and an :class:`httpx.MockTransport` that serves it. Hand-written rather than
mocked, for the reason the whole suite is: a mock asserts that we called what we thought
we called, and this asserts that a token keyring would actually mint is one this service
would actually accept.

The ``kid`` is computed the way keyring computes it -- ``sha256(f"{n}:{e}")``, first
sixteen hex characters -- so a test that changes the key gets a different ``kid`` for the
same reason a keyring restart with a replaced key would.

Every fetch is counted. That counter is what makes the rate-limit test mean something:
the property under test is not "a refetch happens" but "no more than one refetch happens
per window, however many unknown kids arrive".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

if TYPE_CHECKING:
    from collections.abc import Iterable

ALGORITHM = "RS256"
KEY_SIZE_BITS = 2048
PUBLIC_EXPONENT = 65537

ISSUER = "https://keyring.test"
AUDIENCE = "persona"


def _b64url(value: int) -> str:
    """Encode a JWK integer: big-endian bytes, base64url, no padding."""
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


class FakeKeyring:
    """One signing key, and the JWKS document that verifies it."""

    def __init__(self) -> None:
        self._private = rsa.generate_private_key(
            public_exponent=PUBLIC_EXPONENT, key_size=KEY_SIZE_BITS
        )
        numbers = self._private.public_key().public_numbers()
        material = f"{numbers.n}:{numbers.e}".encode()
        self.kid = hashlib.sha256(material).hexdigest()[:16]

    @property
    def public_pem(self) -> bytes:
        """The verifying half, for the algorithm-confusion test that signs with it."""
        return self._private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def jwks(self) -> dict[str, Any]:
        """The document keyring publishes, field for field."""
        numbers = self._private.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": ALGORITHM,
                    "kid": self.kid,
                    "n": _b64url(numbers.n),
                    "e": _b64url(numbers.e),
                }
            ]
        }

    def mint(
        self,
        *,
        subject: str = "acct_one",
        audience: str = AUDIENCE,
        issuer: str = ISSUER,
        issued_at: datetime,
        lifetime_seconds: float = 900.0,
        kid: str | None = None,
        drop: Iterable[str] = (),
    ) -> str:
        """Mint a token exactly as keyring's ``TokenSigner.issue`` would.

        Args:
            subject: the opaque account id, which becomes the token's ``sub``.
            audience: which service the token is for. A token minted for another one
                must be refused here, and that is what the ``aud`` claim is for.
            issuer: which keyring minted it.
            issued_at: the clock reading at minting. Taken as an argument rather than
                read, because the tests move time and nothing may read a wall clock.
            lifetime_seconds: how long before it expires.
            kid: override the key id in the JOSE header, for the unknown-kid tests.
            drop: claims to leave out, for the missing-claim tests.
        """
        claims: dict[str, Any] = {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + timedelta(seconds=lifetime_seconds)).timestamp()),
        }
        for claim in drop:
            claims.pop(claim, None)

        private_pem = self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(
            claims, private_pem, algorithm=ALGORITHM, headers={"kid": kid or self.kid}
        )


def forge(header: dict[str, Any], claims: dict[str, Any], *, secret: bytes | None) -> str:
    """Build a JWS by hand, the way somebody attacking this service would.

    PyJWT refuses to *encode* HS256 with a PEM key -- that guardrail is the library
    protecting its own users from the algorithm-confusion attack. An attacker is not
    using PyJWT, so the forgeries in the test suite are assembled from base64 and hmac
    directly. Anything less would be testing PyJWT's encoder rather than our verifier.

    Args:
        header: the JOSE header, verbatim. May carry any ``alg``, any ``kid``, or none.
        claims: the payload, verbatim.
        secret: the HMAC key for an HS256 signature, or ``None`` for no signature at all
            -- which is what ``alg: none`` means.
    """
    segments = [
        base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=")
        for part in (header, claims)
    ]
    signing_input = b".".join(segments)
    signature = b"" if secret is None else hmac.new(secret, signing_input, hashlib.sha256).digest()
    segments.append(base64.urlsafe_b64encode(signature).rstrip(b"="))
    return b".".join(segments).decode()


class FakeJwksEndpoint:
    """Serves a key set over :class:`httpx.MockTransport`, and counts the fetches.

    ``fetches`` is the whole point. The rate-limit test asserts on this number, because
    the property is not "an unknown kid provokes a refetch" -- it is "however many
    unknown kids arrive, keyring is fetched at most once per window".
    """

    def __init__(self, keyring: FakeKeyring) -> None:
        self.keyring = keyring
        self.fetches = 0
        self.fail_with: Exception | None = None
        self.body: str | None = None
        self.status_code = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.fail_with is not None:
            raise self.fail_with
        body = self.body if self.body is not None else json.dumps(self.keyring.jwks())
        return httpx.Response(self.status_code, text=body, request=request)

    def serve(self, keyring: FakeKeyring) -> None:
        """Replace the key set, as a keyring whose key was replaced would."""
        self.keyring = keyring
        self.body = None
