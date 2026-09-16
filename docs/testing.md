# Testing

`make check` is the gate: format, lint, strict mypy over `src` **and** `tests`, the five
layering contracts, and the suite at **100% branch coverage with no `# pragma: no cover`**.
`make matrix` runs the tests on every Python CI runs, because a green `check` is one
interpreter's opinion and coverage genuinely differs between versions.

The conventions are keyring's, adopted wholesale: tests named after the behaviour in full
sentences, `tests/unit/<layer>/` mirroring `src/`, hand-written fakes that satisfy the real
`Protocol`, `FakeClock` instead of sleeping, no `unittest.mock`, and comments that explain
*why a property matters* rather than what the code does.

What follows is the part worth reading: **each test group, and the specific failure it
exists to catch.** A group whose failure mode cannot be named should not be a group.

## Domain

The most heavily tested layer, because it is pure and because the expensive mistakes live
here.

**Key normalization — the sprawl table.** `"Favourite Topics"`, `"favourite-topics"`,
`"FAVOURITE_TOPICS"`, `"  favourite   topics  "`, `"favourite__topics"` and
`"--favourite-topics--"` must all collapse to exactly `favourite_topics`. This is the
anti-sprawl mechanism, and it fails by being *almost* right: one of the six mapping
somewhere else means a persona holding the same fact under two keys, with nothing to say
so.

**Value type derivation.** `derive_value_type` checks `bool` *before* `int`. In Python
`isinstance(True, int)` is `True`, so the obvious ordering types every boolean as a number,
silently, forever. There is a test that says exactly that and a comment explaining why.

**Every value limit, and the message naming it.** Each of bytes, depth, list length and
object width, tested just under and just over, with an assertion on *which* limit the
message names. A caller told only "invalid" has to bisect its own payload.

**The two credential corpora.** This pair *is* the specification for
[ADR-0002](adr/0002-no-secrets.md):

- `MUST_BE_ACCEPTED` — sentences a person would actually want remembered, including ones
  containing a git sha, a URL, a hex colour, a long compound word, a file path, CJK text
  and emoji. A false positive here blocks a legitimate memory **and there is no override**.
- `MUST_BE_REFUSED` — credential shapes, including ones embedded mid-sentence.

When the heuristic is tuned, it is tuned so both pass. **Never by deleting a corpus
entry.** An entry that genuinely cannot be classified gets a comment saying so, and stays.

## Auth

The highest-value group in the project — and most of what it defends is not tested here.

The rules that decide whether a token is good — the pinned algorithm, the pinned issuer, the
required claims, expiry on the injected clock, tampering, malformed input — and every rule
about fetching keyring's keys — the cache, the refetch floor for an unknown `kid` (the
amplifier), the floor after a failed fetch, stale keys through an outage — belong to
`keyring_client`, the verifier every service in the family shares. They are tested
exhaustively in the keyring repository, asserting on fetch *counts* where the property is a
count, and against keyring's own signer. A second copy of that suite here would only drift
from the first.

What is tested here is what this service does with them:

| Class | What it catches |
| --- | --- |
| `TestTheSharedRulesAreWiredIn` | The adapter handing the shared verifier somebody else's issuer, clock or keys. `alg: none`, and HS256 signed with the JWKS **public** key, are both still refused in this service's own suite. |
| `TestAudience` | A token for another service being accepted — including `persona.work`, which an audience *family* would accept and this service must not. The `aud` claim is the entire reason keyring mints per-service tokens. |
| `TestAnUnknownKeyId` | A `kid` missing from the key set keyring has just served coming back as a 503. Keyring answered, and its answer is about the token: a 401. |
| `TestKeyringUnreachable` | Startup needing keyring; keyring being down turning into a 401, which would send the caller to re-authenticate against a service that is not answering; the 503 naming the host. |
| `TestHealth` | `/ready` reporting a guess. It asks keyring itself, is **degraded** only when no token could be verified, and says so — `reachable: false`, with a reason — while an outage is being survived on cached keys. |

Plus one test at each level asserting **every** refusal says the same thing: one message out
of the verifier, and a byte-identical 401 body apart from the request id over HTTP. A caller
holding a forged token learns nothing from which rule refused it.

The fake is `keyring_client.testing`, through `tests/fakes/keyring.py`: a real RSA key, a
real JWKS document served through an `httpx.MockTransport`, and forgeries assembled by hand.
No network, no `unittest.mock`.

## Isolation

One test per verb, the way keyring does it. Account B cannot read, list, search, export,
set, revise, forget or event-log account A's persona, and gets **the same 404 it would get
for a persona that never existed**.

`TestSearchIsolation` gets its own class, because the full-text index is the one place
where isolation is not structural: one `fields_fts` and one `notes_fts` hold every
account's text, and the account filter lives in the `JOIN` rather than in the `MATCH`. That
works — and it is exactly the kind of thing that breaks silently, which is what the class
is for.

## Retrieval

Every filter alone and in combination; cursor pagination across a write (no row appears
twice, none is skipped); the `?keys=` batch fetch; export on a large persona;
`recall_everywhere` spanning two profiles; forgotten rows absent by default and present
with `include_forgotten`.

The pagination test is the one that earns its place. Offset pagination shifts under a
concurrent write; the cursor encodes the last `(sort_key, id)` seen, so the test walks a
persona, writes into the middle of it, finishes the walk, and asserts the multiset of ids
is exactly right.

## Hostile search

Parametrized over the ten strings below, each asserting a clean 200 or 422 and **never a
500**. Measured on this exact stack before the sanitiser existed, eight of ten raised
`OperationalError` out of FTS5:

```
'answers"'     -> unterminated string
'concise OR'   -> fts5: syntax error near ""
'NEAR('        -> fts5: syntax error near ""
'*'            -> unknown special query
''             -> fts5: syntax error near ""
```

The fix is to extract word tokens with `re.findall(r"[^\W_]+", query, re.UNICODE)`,
double-quote each and join with ` OR `, so `'x AND OR y'` becomes
`"x" OR "AND" OR "OR" OR "y"` and returns rows instead of raising. A query with no word
tokens is a clean 422, not a crash.

## Index integrity

The full-text index is written by the store, explicitly, in the same transaction as the
row — not by external-content tables and not by triggers, both for reasons in
[docs/architecture.md](architecture.md). That choice creates its own bug class: an index
that silently disagrees with the table.

So it gets its own class, asserting the table and the index agree after **create, revise,
forget, and persona delete**. The last one is the one that would be missed.

## Provenance

`asserted_by` comes from the verified token, and a request body claiming otherwise is
ignored — the test sets one and asserts it had no effect. `source` round-trips as the claim
it is. [ADR-0004](adr/0004-provenance-is-partly-a-claim.md)

## Caps

Each cap, plus an `asyncio.gather` concurrency test for each, asserting the cap still
holds. A cap checked by the caller before the write has a window between the count and the
write that two concurrent writes both pass; these tests are what makes "enforced inside the
transaction" a fact rather than a comment.

## Concurrency

`database_held` and `park_behind_the_database` are copied from
`Keyring-api/tests/unit/accounts/test_roles_store.py`. They occupy the database's only
worker thread so a test can line several calls up and know that **none of them has read
anything yet** — which is the interleaving in which a check made outside a transaction
reads stale state and every writer goes ahead.

Two writers to the same field key: one row, a revision bump, no lost write. The assertion
is on the **outcome**, never the mechanism, so it would still mean something if the single
connection were ever replaced by a pool.

## Restart

The `running()` harness is copied from `Keyring-api/tests/integration/test_restart.py`:
each test runs two apps in sequence over one database file, exactly as a restart does.

Persona, fields, notes, pins, events and **searchability** all survive. The last is the one
that could regress alone — an FTS index rebuilt into a temporary table would pass every
other assertion here.

Including the negative: **a forgotten note stays forgotten.** A restart that resurrects
tombstoned rows is worse than one that loses them.

## Contract

The `operation_id` set, pinned literally. Renaming one is a breaking change for every MCP
client with a tool bound to it, so the set is written out rather than left to drift.

Plus: every operation has a summary and a description longer than forty characters, so an
undescribed route cannot ship; and a walk over every response schema in `/openapi.json`
asserting **no field could carry a credential** — checked against the declared contract
rather than one sampled response, so a field added later is caught by the suite rather than
by whoever is reading the logs.

## Preferences

settings-api is its shared `FakeSettingsClient`, including with `unavailable = True`,
because the outage is the case most services forget. A person may narrow a pin or recall
cap and never raise it; a 401/403 from settings-api is a 503 with fixed text that names
neither the grant nor the URL; a value of the wrong type leaves the configuration and
logs the key, never the value. Two accounts on one store are held to different pin
ceilings because the cap is a call argument, not a constructor-frozen integer.

`default_persona`, `log_values`, `erasure_mode` and `grace_days` are unread on purpose:
this service has no default-persona resolution and no sweeper, and faking either would
be a setting that stores a value and changes nothing.

## What is deliberately not tested

- **That the MCP layer renders memories as claims.** It cannot be — there is no MCP layer
  yet, and the API cannot enforce it. [ADR-0001](adr/0001-data-not-instructions.md) says so
  plainly rather than pretending otherwise.
- **That `source` is truthful.** It is a claim. [ADR-0004](adr/0004-provenance-is-partly-a-claim.md).
- **That a profile exists in keyring.** persona-api cannot ask.
  [ADR-0005](adr/0005-one-persona-per-profile.md).
