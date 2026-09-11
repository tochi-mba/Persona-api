# ADR-0005: One persona per profile — and we cannot check the profile exists

**Status:** accepted.

## Context

An account might want more than one assistant. A work assistant that knows about
deadlines and a home assistant that knows about the dog should not share a voice, a set
of forms of address, or a memory of what went wrong last Tuesday.

keyring already has the vocabulary for this: a **profile** is a named credential set an
account owns — "personal", "work". The natural key is therefore `(account_id, profile)`.

## Decision

**One persona per `(account_id, profile)`.** It is the primary key of the `personas`
table, and every field and note is keyed by it too, with `ON DELETE CASCADE` back to it.

Not one per account — that gives one assistant per person and no way to separate contexts.
Not free-form personas with their own ids — that is a second naming system to keep in sync
with keyring's, and nothing would keep it in sync.

## The honest part: persona-api cannot verify a profile exists

This is the consequence that has to be written down, because it looks like a bug.

persona-api authenticates by verifying keyring's signed token **locally** — see
[ADR-0007](0007-local-jwks-verification.md). The token carries `iss`, `sub`, `aud`, `iat`,
`exp`. It does not carry a profile list, and keyring exposes **no endpoint that would
answer "does this account have a profile called X"**. There is nothing to ask, and asking
would defeat the point of local verification anyway.

So the profile is an **opaque namespace string supplied by the caller**, validated for
*shape* only: lowercase, ≤ 64 characters, the same character class keyring's own profile
names use.

The consequence: **a typo makes a new empty persona rather than an error.** Write to
`wrok` instead of `work` and you get a second, empty persona, silently.

## Why that is acceptable, and what makes it survivable

Three things:

- `GET /v1/personas` lists every persona the account owns, so a typo is visible the moment
  anybody looks. It is the first thing an assistant should call when a persona seems
  emptier than expected.
- `DELETE /v1/personas/{profile}` is a hard delete and cascades, so cleaning up a typo is
  one call.
- The per-account persona cap (default 20) bounds how much damage a loop of typos can do.

The alternative — a per-request call to keyring to validate the profile — would put a
network hop on every single write, make persona-api unavailable whenever keyring is, and
require keyring to grow an endpoint whose only consumer is this validation. That is a much
larger cost than an occasional empty persona.

## What it costs beyond that

Two assistants sharing one profile share one persona. That is the intended behaviour and
it is what "per profile" means, but it is worth saying out loud: the separation this gives
you is as fine-grained as your profiles are, and no finer.

## What would change our minds

keyring growing an endpoint that lists an account's profiles, for reasons of its own. Then
the honest design would be to validate **lazily and advisorily** — create the persona
anyway, but report `profile_unknown_to_keyring: true` on the response — rather than to
refuse, because a persona service that goes down when keyring does is a worse trade than a
stray empty persona.
