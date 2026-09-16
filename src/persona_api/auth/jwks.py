"""Keyring's public keys, through the family's shared client.

persona-api never calls keyring at request time. It verifies keyring's signed tokens
locally against the public keys keyring publishes, which is exactly what that endpoint is
for -- see ``docs/adr/0007-local-jwks-verification.md``. Fetching those keys, caching them,
the single-flight lock, the rate limit on unknown key ids, the floor after a failed fetch,
and the grace for cached keys through a keyring outage all live in
:class:`keyring_client.JwksClient`, in the keyring repository, where they are tested
against keyring's real JWKS endpoint. This module keeps the import path the rest of this
service uses, and nothing else.

Three things about that client are load-bearing from here, and none of them is obvious from
the call site:

**Nothing is fetched while starting up.** A persona service that will not start because
keyring is down is a persona service that cannot report keyring being down.

**"Keyring is down" and "that token is not ours" are different answers.** A fetch that
fails, with no usable copy of the keys held, is a 503. A fetch that works and comes back
without the token's key id is a 401: keyring has just said that no key by that name is its
own, which is a fact about the token rather than an outage.

**Keys held from a successful fetch are served through a short outage.** That is why
``/healthy`` can report keyring unreachable while still reporting itself ok.

The messages are re-exported because they are contract: every token refusal says
:data:`BAD_TOKEN`, keys that could not be fetched say :data:`KEYS_UNAVAILABLE`, and
``/healthy`` reports :data:`KEYS_STALE` verbatim while it is surviving an outage.
"""

from __future__ import annotations

from keyring_client import BAD_TOKEN, KEYS_STALE, KEYS_UNAVAILABLE, JwksClient

__all__ = ["BAD_TOKEN", "KEYS_STALE", "KEYS_UNAVAILABLE", "JwksClient"]
