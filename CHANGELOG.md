# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Optional settings-api wiring, off unless both `PERSONA_SETTINGS_API_BASE_URL` and
  `PERSONA_SETTINGS_API_TOKEN` are set. When on, each request reads that caller's
  `persona.recall_default_limit`, `persona.max_pinned_fields` and `persona.max_pinned_notes`
  and holds them to the deployment's ceilings -- a person may narrow a cap and never raise
  it. Unset, behaviour is unchanged. `default_persona`, `log_values`, `erasure_mode` and
  `grace_days` stay in the catalogue and unwired until this service grows a default-persona
  resolution and a sweeper.

### Changed

- CI inherits `FAMILY_GITHUB_TOKEN`; image builds accept a BuildKit `github_token`
  secret so tagged client packages can be fetched from private family repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- **Breaking:** `GET /healthy` is liveness only -- the process is running, no I/O, and it
  never fails, so it no longer asks keyring anything. The database and keyring checks moved
  to a new `GET /ready` (`check_readiness`), which answers 503 when no token could be
  verified. This is what stops an orchestrator restarting a healthy container through
  keyring's outage. Point container healthchecks at `/healthy` and load balancers at `/ready`.
- **Breaking:** the default port is 8004, the one this service is assigned in the family's
  port table, rather than 8002, which it shared with user-api.
- Token verification comes from `keyring-client` (`Keyring-api/clients/python`), the library
  every service in the family shares, instead of a JWKS client and verifier of this service's
  own. The rules are the ones persona-api already applied -- RS256 only, the issuer pinned,
  the audience pinned to exactly `persona`, every claim keyring sets required, expiry on the
  injected clock, one undifferentiated `401` -- and they are now tested once, in the keyring
  repository, against keyring's own signer. `make install` needs that repository checked out
  beside this one.
- **Breaking:** a token whose `kid` is not in the key set keyring has just served is refused
  with `401`, where it used to be `503`. Keyring answered, and its answer was that no key by
  that name is its own: a fact about the token, which "retry shortly" could never fix. A key
  fetch that fails is still `503`.
- A failed key fetch is not retried until `PERSONA_JWKS_MIN_REFETCH_SECONDS` has passed -- the
  floor an unknown `kid` already had -- so a keyring that is down is not asked once per
  inbound request.
- Keys held from a successful fetch keep verifying tokens through a keyring outage for up to a
  day past `PERSONA_JWKS_CACHE_SECONDS`. Previously every request that needed a fetch failed
  with `503` as soon as the cached key set reached that age.
- `GET /healthy` fetches keyring's keys itself when it holds none fresh, rather than reporting
  what earlier requests happened to find, and the keyring check gains `detail.reason`.
  `detail.reachable` is always `true` or `false`; it used to be `null` until something needed
  a key. The check is `degraded`, and the response `503`, only when no token could be
  verified: an outage survived on cached keys is `ok`, with `reachable: false` and a reason
  saying so. A fresh process whose keyring is down is therefore `degraded` from its first
  health check, and the image's `HEALTHCHECK` reports it unhealthy until keyring answers.
- A `PERSONA_AUDIENCE` that is empty, has surrounding whitespace or contains a dot stops the
  service starting, instead of letting it run and refuse every token it is sent.
- The import-linter contract that keeps keyring behind `persona_api.auth` now forbids
  `keyring_client` everywhere else, as well as `jwt` and `httpx`.

### Security

- Tokens keyring never mints are refused where they used to be accepted: an `aud` that is a
  list containing `persona`, an empty `sub`, or an `exp` that is not a number.

### Fixed

- A token whose `iat` was ahead of this host's wall clock -- keyring's clock running slightly
  fast -- was refused, because PyJWT's own `iat` check read the wall clock. Only expiry is
  judged now, and on the injected clock.
- The README, `.env.example`, `docs/operations.md` and `scripts/smoke.py` said keyring must
  list `persona` in `KEYRING_SERVICE_TOKENS` before it would mint a token for that audience.
  It does not: keyring mints for any audience it is asked for, and `KEYRING_SERVICE_TOKENS`
  only admits services to keyring's `/v1/internal` endpoints, which persona-api never calls.
  persona-api needs no entry there.
- The `curl` examples for minting a token now send `Content-Type: application/json`, without
  which keyring cannot read the request body and answers `422`.
