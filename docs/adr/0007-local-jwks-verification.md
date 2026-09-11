# ADR-0007: Verify keyring's tokens locally, not by asking keyring

**Status:** accepted. Inherits the trade in
[keyring's ADR-0008](https://github.com/tochi-mba/Keyring-api/blob/main/docs/adr/0008-opaque-sessions-signed-service-tokens.md).

## Context

persona-api needs to know which account a request is for. keyring holds the accounts. The
two ways to connect those facts are:

1. **Ask keyring on every request.** Send the token, get back an account id.
2. **Verify keyring's signed token locally** against the public key it publishes.

keyring was built for the second — that is exactly what `POST /v1/auth/service-token` and
the JWKS endpoint are *for*, and its ADR-0008 already paid for the design.

## Decision

**persona-api never calls keyring at request time.** A caller presents
`Authorization: Bearer <RS256 JWT>`, minted by keyring with `{"audience": "persona"}`, and
this service verifies it locally against the JWKS.

The verification mirrors keyring's own `signing.py:113-135` precisely:

```python
jwt.decode(
    token,
    key,  # from the JWKS, matched on the header's kid
    algorithms=["RS256"],  # a fixed list. Never read alg from the token.
    audience=settings.audience,  # "persona"
    issuer=settings.keyring_issuer,
    options={"require": ["exp", "iat", "iss", "sub", "aud"], "verify_exp": False},
)
# then, separately:
if clock.now().timestamp() >= float(claims["exp"]):
    raise AuthenticationError(BAD_TOKEN)
```

`algorithms=["RS256"]` is a fixed list, never read from the token: leaving it open is the
classic JWT failure, where a caller supplies `alg: none` or downgrades RS256 to HS256 and
signs with the public key everyone can fetch. There is a test for each.

`verify_exp=False` plus an explicit check against the **injected clock** is not pedantry.
It is the codebase invariant — nothing reads the wall clock directly — and it is what makes
the expiry rule testable at all: with PyJWT doing it, a test could only ever assert that a
token minted now is valid now.

Every failure — bad signature, wrong audience, wrong issuer, expired, missing claim —
collapses to one undifferentiated `AuthenticationError`. 401, same body every time, apart
from the request id.

## What we inherit, and must not design around

**A signed token is not revocable.** Logging out of keyring does not end access here for up
to keyring's `access_token_ttl_seconds` (15 minutes by default). That is the accepted trade
for local verification, it is keyring's trade rather than ours, and the right response to
wanting it shorter is to shorten keyring's TTL — not to add a revocation check that would
reintroduce the per-request call.

**persona-api cannot ask keyring anything about an account.** There is no endpoint for it.
In particular it cannot verify that a profile exists — see
[ADR-0005](0005-one-persona-per-profile.md).

## The JWKS client, and the one thing that needed care

- Fetched **lazily, on first use**. Startup must not require keyring to be reachable: a
  persona service that will not start because keyring is down is a persona service that
  cannot report keyring being down. `/healthy` reports keyring reachable/unreachable as
  **degraded**, the same shape keyring uses for a sealed vault.
- Cached by `kid` for `jwks_cache_seconds`.
- **An unknown `kid` triggers a refetch, rate-limited to one per
  `jwks_min_refetch_seconds`.** This is the part that is not a tuning knob. Without the
  limit, anyone can force one outbound fetch per request by sending tokens with random
  `kid` headers — a DoS amplifier, with this service's credentials on it, pointed at
  keyring. It is tested by asserting the fetch *count* under a stream of bogus kids.
- keyring has no key rotation and its `kid` is a thumbprint of the key, so it is stable
  across restarts. A changed `kid` means the key was replaced, and is picked up on refetch.

## Why the whole thing lives behind one import-linter contract

Nothing outside `persona_api.auth` may import `jwt` or `httpx`. Everything this service
asks of keyring, and every rule by which it believes an answer, is therefore inside one
module a reviewer can read in a sitting. Without the contract, the day somebody needs a
fact keyring holds, an `httpx` call appears in a router and the boundary is gone before
anyone notices.

## What would change our minds

keyring gaining real key rotation, or a revocation list worth consulting. Neither changes
the local-verification decision; both would change what the JWKS client does between
fetches.
