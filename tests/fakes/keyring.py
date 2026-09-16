"""A keyring that exists entirely inside the test process: the family's shared fake.

The real RSA key, the JWKS document in keyring's exact shape, the ``httpx.MockTransport``
that serves it and counts every fetch, and the forgeries assembled by hand all live in
:mod:`keyring_client.testing`. Every service in the family tests against that one fake, and
it is itself checked against keyring's real app in the keyring repository, so the fakes
cannot drift apart. Hand-written rather than mocked, for the reason the whole suite is: a
mock asserts that we called what we thought we called, and this asserts that a token keyring
would actually mint is one this service would actually accept.

This module only sets this service's defaults. Tokens are minted for the ``persona``
audience with keyring's own fifteen-minute lifetime, which is what the expiry tests count
down from; a test that moves the clock further passes its own ``ttl_seconds``.
"""

from __future__ import annotations

from typing import Any

from keyring_client.testing import (
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    SIGNING_KEY,
    FakeKeyring,
    jwks,
)
from keyring_client.testing import forge_hs256 as _forge_hs256
from keyring_client.testing import forge_unsigned as _forge_unsigned
from keyring_client.testing import mint as _mint

__all__ = [
    "AUDIENCE",
    "DEFAULT_TTL_SECONDS",
    "ISSUER",
    "JWKS_URL",
    "ROTATED_KEY",
    "SIGNING_KEY",
    "FakeKeyring",
    "forge_hs256",
    "forge_unsigned",
    "jwks",
    "mint",
]

AUDIENCE = "persona"
"""The one audience this service answers to."""

DEFAULT_TTL_SECONDS = 900
"""Keyring's own access token lifetime. Short, because a signed token cannot be revoked."""


def mint(
    *, audience: str = AUDIENCE, ttl_seconds: float = DEFAULT_TTL_SECONDS, **overrides: Any
) -> str:
    """Mint a token the way keyring's ``issue_service_token`` does, for this service."""
    return _mint(audience=audience, ttl_seconds=ttl_seconds, **overrides)


def forge_hs256(*, account_id: str = "acct_attacker", audience: str = AUDIENCE) -> str:
    """The algorithm-confusion attack: HS256, signed with the published public key."""
    return _forge_hs256(account_id=account_id, audience=audience)


def forge_unsigned(*, account_id: str = "acct_attacker", audience: str = AUDIENCE) -> str:
    """``alg: none`` with an empty signature: the other half of the same attack."""
    return _forge_unsigned(account_id=account_id, audience=audience)
