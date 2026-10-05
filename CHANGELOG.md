# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The four `persona` settings the catalogue proposed are honoured, per person, from
  settings-api ([ADR-0009](docs/adr/0009-erasure-and-the-default-persona.md)). A person who
  chose none of them -- and every deployment without settings-api -- sees no change.
  - `persona.erasure_mode` and `persona.grace_days`: `forget_field` and `forget_note` keep
    the row (`tombstone`), schedule its destruction (`grace`, after `grace_days`), or
    destroy it before responding (`immediate`). The choice is written on the row as
    `purge_after` when it is forgotten, so a later change of mind never reaches back.
    Forgotten fields and notes carry `purge_after` in responses. Setting a forgotten field
    again inside its grace period revives it and calls the destruction off.
  - A sweeper destroys what is due: at startup and every `PERSONA_PURGE_INTERVAL_SECONDS`
    (3600), at most 500 fields and 500 notes per sweep, needing no token.
  - `persona.log_values`: a change that replaces a field's value or a note's body keeps
    what it replaced in the event's new `old_value`. Whatever destroys the row -- the
    sweeper, an immediate erasure, a persona delete -- strips it in the same transaction.
  - `persona.default_persona`: `@default` in any `{profile}` path names the person's
    default persona. With none chosen it is refused with the same 422 as before.
- Migration `0002_erasure.sql`: `fields.purge_after`, `notes.purge_after`,
  `events.old_value`, and partial indexes for the sweep and the strip. Every existing row
  reads back as it did: a forgotten row stays a tombstone.
- A GitHub Pages site at <https://tochi-mba.github.io/Persona-api/>, in the REX ink/signal style: what Persona-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- Optional settings-api wiring, off unless both `PERSONA_SETTINGS_API_BASE_URL` and
  `PERSONA_SETTINGS_API_TOKEN` are set. When on, each request reads that caller's
  `persona.recall_default_limit`, `persona.max_pinned_fields` and `persona.max_pinned_notes`
  and holds them to the deployment's ceilings -- a person may narrow a cap and never raise
  it. Unset, behaviour is unchanged.

### Changed

- The database runs with `PRAGMA secure_delete = ON`. Anything that destroys a row (an
  immediate erasure, a sweep, a write to a field whose grace period has ended, a persona
  delete) also rewrites the full-text index it was in with FTS5 `optimize`, in the same
  transaction, and then truncates the write-ahead log. Without the rewrite a destroyed
  row's words stayed in the index's segments, readable with `grep`.
- `forget_field` and `forget_note` read the caller's settings when settings-api is on, so
  settings-api refusing this service (401/403) makes them 503, as it already did every
  route that reads a pin cap.
- **settings-client 0.4.1**, whose single-flight locks no longer outlive a failed resolve:
  during a long settings-api outage the client kept one lock per token it had seen.
- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- CI calls the family's reusable workflow and fetches private family packages through its
  OIDC token broker (`id-token: write`), with no long-lived token in this repository; image
  builds accept a BuildKit `github_token` secret so tagged client packages can be fetched
  from private family repositories.
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
  repository, against keyring's own signer. It is taken from a tagged git source, so
  `make install` needs no keyring checkout beside this one.
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
- `GET /ready` fetches keyring's keys itself when it holds none fresh, rather than reporting
  what earlier requests happened to find, and the keyring check gains `detail.reason`.
  `detail.reachable` is always `true` or `false`; it used to be `null` until something needed
  a key. The check is `degraded`, and the response `503`, only when no token could be
  verified: an outage survived on cached keys is `ok`, with `reachable: false` and a reason
  saying so. A fresh process whose keyring is down is therefore `degraded` from its first
  readiness check. The image's `HEALTHCHECK` calls `/healthy`, so it stays healthy.
- A `PERSONA_AUDIENCE` that is empty, has surrounding whitespace or contains a dot stops the
  service starting, instead of letting it run and refuse every token it is sent.
- The import-linter contract that keeps keyring behind `persona_api.auth` now forbids
  `keyring_client` everywhere else, as well as `jwt` and `httpx`.

### Security

- Tokens keyring never mints are refused where they used to be accepted: an `aud` that is a
  list containing `persona`, an empty `sub`, or an `exp` that is not a number.

### Fixed

- **settings-client 0.4.2.** A 2xx answer the client cannot use -- a proxy's page, an empty
  body, a document from a newer settings-api -- is treated as an outage and degrades as one,
  instead of reaching this service as a 500.
- **settings-client 0.2.0**, and the profile goes with every read. `persona.recall_default_limit`
  is profile-scoped in settings-api, and 0.1.0 could not name a profile, so every read got the
  catalogue default and a person's own recall default was never applied. Requests under
  `/v1/personas/{profile}/...` now ask for that profile (in its stored form);
  `GET /v1/recall` spans every profile and asks for none.
- `scripts/smoke.py` read its target from `PERSONA_URL`, a name under this service's own
  prefix: exported in the shell that starts persona-api, it made the service refuse to start
  with an unknown-variable error. It is now `SMOKE_PERSONA_API_URL`, matching user-api's
  `SMOKE_USER_API_URL`, and a test checks every variable the script reads against the
  startup check.
- `make matrix` ran 3.11 and 3.12. 3.11 is below `requires-python`, so uv refused it and
  the target failed before a test ran; it now runs 3.12 and 3.13, which is what CI runs.
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
