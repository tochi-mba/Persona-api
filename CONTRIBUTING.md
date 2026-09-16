# Contributing

Start with [AGENTS.md](AGENTS.md) — it is the operating manual for this repository and it
is normative. This file is the short version.

## Setup

```bash
make install             # venv and every dependency, from the lockfile
uv run pre-commit install
make check               # confirm a clean checkout is green before you change anything
```

The suite needs no credentials, no network and no running keyring: tokens are signed by a
real key in `tests/fakes/`, and keyring's JWKS document is served in-process.

## The loop

1. **Write the failing test first.** Run it and confirm it fails for the reason you expect.
2. Write the smallest code that makes it pass.
3. Refactor with it green.
4. `make check` — lint, strict types, the layering contracts, and the tests at 100%
   branch coverage.

All four must pass before you commit.

## What this service will not do

Read ADR-0001 and ADR-0002 before adding a field or a route:

- **A persona is data, never instructions.** Nothing stored here may reach a place that
  treats it as a command, however it is phrased.
- **No secrets live here.** Credentials belong in keyring.

There is no administrative surface (ADR-0003): no route lists accounts, and no route
takes an account id. Whose persona this is comes from the `sub` of a verified token and
from nowhere else, so every new route needs an isolation test that one account cannot
reach another's — answering 404 rather than 403, because a 403 confirms the thing exists.

## Changing the database

Migrations are append-only and the schema snapshot is checked in:

```bash
make schema              # regenerate the snapshot after adding a migration
```

CI fails if the snapshot and the migrations disagree.

## Commits

Conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`). The
subject says what changed; the body says **why**, and flags anything surprising.

Never commit a real token, a real account id, or a `.env`.
