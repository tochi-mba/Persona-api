# ADR-0003: No administrative surface at all

**Status:** accepted.

## Context

keyring has an administrative surface and needs one: somebody has to issue the first
invite, disable a compromised account, and read the audit log. It carries RBAC, four
built-in roles, a break-glass token, an escalation-resistant permission model and a set of
tests written as an attacker. That machinery is most of what makes keyring a large service.

The obvious move is to mirror it here. It should be resisted, and this ADR is why.

## Decision

**There is no administrative surface in persona-api.** No admin API. No roles, no
permissions, no RBAC. No break-glass token. No operator endpoint that reads somebody's
persona on their behalf.

Every row in this service is reachable only by the account named in the `sub` of a
verified keyring token. There is exactly one identity in the system and it is the caller's.

If you want somebody's persona, **ask them**.

## Why this is not an omission

Ask what an administrator would *do* here.

- **Onboard someone?** There is nothing to onboard. A persona springs into existence the
  first time an account writes to a profile.
- **Recover access?** Access is a keyring token. Recovery is a keyring problem, and
  keyring solves it.
- **Disable an account?** Same: stop minting it tokens. This service holds no separate
  credential that could be revoked here.
- **Audit what somebody recorded?** That is the one that sounds reasonable, and it is the
  one that must not exist. "Read what the assistant has written about this person" is
  surveillance with an operations-shaped excuse. The event log answers *what changed and
  when*, to the account that owns it, and contains no field value and no note body.
- **Delete somebody's data on request?** They can: `DELETE /v1/personas/{profile}` is a
  hard delete and cascades. An operator doing it for them would need a way to read whose
  it was.

The whole administrative surface evaporates when you ask what it is for. What is left is
"read somebody else's persona", which is the one thing this service must never do.

## What it buys

This service is a fraction of keyring's size, and the reason is entirely this decision.
There is no permission check to get wrong, no ordering rule between permission and
existence, no escalation path to close, no break-glass token to leak, and no 403/404
distinction to leak through — because there is no 403. **Cross-account access is always
404, identical to a persona that never existed.**

The isolation test suite is correspondingly simple and correspondingly complete: one test
per verb, asserting that account B gets the same 404 for account A's persona as it would
for one nobody has ever created.

## What it costs

- **No operator recovery.** If somebody loses their keyring account they lose their
  persona with it. That is the honest consequence and it is written in
  [docs/operations.md](../operations.md).
- **No fleet-wide answer.** "How many people have pinned more than ten fields" cannot be
  answered from the API. It can be answered from the database by whoever runs the box,
  which is the right place for it to be awkward.

## What would change our minds

A legal retention or discovery obligation, which would be a different service with a
different threat model and a different ADR. Not convenience, and not "an admin panel would
be useful" — those are exactly the arguments this record exists to answer.
