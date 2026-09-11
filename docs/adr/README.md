# Architecture decision records

One file per decision that future-us would otherwise re-litigate. Each says what was
decided, what it cost, and what would make us change our minds.

The first four are the line this service does not cross. They are not style preferences;
each one is an invariant in [AGENTS.md](../../AGENTS.md) and at least one test.

| ADR | Decision |
| --- | --- |
| [0001](0001-data-not-instructions.md) | A persona is data, never instructions |
| [0002](0002-no-secrets.md) | No secrets in a persona; keyring is next door |
| [0003](0003-no-administrative-surface.md) | No administrative surface at all |
| [0004](0004-provenance-is-partly-a-claim.md) | Provenance is partly a claim, and the API says which part it verified |
| [0005](0005-one-persona-per-profile.md) | One persona per profile — and we cannot check the profile exists |
| [0006](0006-keyword-search.md) | Keyword search, not embeddings |
| [0007](0007-local-jwks-verification.md) | Verify keyring's tokens locally, not by asking keyring |
| [0008](0008-sqlite.md) | SQLite, one file, and why `synchronous` differs from keyring's |
