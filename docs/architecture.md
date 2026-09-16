# Architecture

One process, one SQLite file, seven layers pointing inward, and five import-linter
contracts that refuse a commit which breaks the shape. If you are here to change
something, read [AGENTS.md](../AGENTS.md) first — this document explains *why* the shape
is what it is; that one explains how to work inside it.

## The shape

```
src/persona_api/
  core/      config, clock, logging, request context, version, preferences, the composition root
  domain/    pure types and rules. Imports nothing internal — not even core.
  storage/   the SQLite connection, the migrations, how a datetime becomes a column
  events/    the append-only record of persona changes
  memory/    FieldStore, NoteStore, the FTS index, recall, filters, cursor pagination
  personas/  PersonaStore and PersonaService — the identity card and the cascade
  auth/      token verification: a thin adapter over the family's keyring_client
  api/       FastAPI app, routers, wire schemas, problem+json errors, middleware
```

Dependencies point inward:
`api → auth → personas → memory → events → storage → domain`.
`core` is a shared kernel everything may use, **except `domain`** — which stays free of
even configuration, so the rules about what a persona may hold can be reasoned about with
nothing else loaded.

This is keyring's architecture, deliberately. The two services are meant to be readable by
the same person on the same afternoon, and the second one being shaped differently for no
reason would be a cost paid by every future reader.

## The five contracts

They are enforced by `lint-imports`, which runs inside `make check`. There are five, each
earned.

**1. Layers point inward.** The list above, in order.

**2. The domain imports nothing internal.** `persona_api.domain` may not import `api`,
`auth`, `core`, `events`, `memory`, `personas` or `storage`. What this buys is that key
normalization, value limits and the credential refusal can be read, tested and reasoned
about as pure functions — and the credential refusal in particular is the one piece of
this service where a subtle mistake is expensive, so it is worth having it be the piece
with the fewest moving parts around it.

**3. SQL stays behind the stores.** `api`, `auth` and `domain` may not import
`persona_api.storage` or `sqlite3`. A router that *could* write a query is a router that
will eventually contain one — and the first one will be the one that forgets the
`account_id` in the `WHERE` clause.

**4. Talking to keyring stays behind the auth adapter.** Nothing outside
`persona_api.auth` may import `keyring_client`, `jwt` or `httpx`, except `core.config`,
which validates `PERSONA_SETTINGS_API_TOKEN` with `keyring_client.check_service_token`
rather than a copy of the 32-character rule.

persona-api's entire relationship with keyring is "verify this signed token against those
published keys". That is a small thing, and it should stay small. The rules for it are the
family's, in `keyring_client`; the contract keeps every question this service asks of
keyring, and every rule by which it believes an answer, behind one package that can be read
in a sitting. `keyring_client` is named alongside `httpx` because the same library carries a
client for keyring's internal credential endpoints, which this service has no reason to
call — and importing it is one line.

Without it, the failure is predictable: somebody needs a fact keyring holds — a display
name, a profile list, whether an account is disabled — and an `httpx` call or a
`keyring_client` import appears in a router. It works. It is reviewed and merged. And the
boundary that made [ADR-0007](adr/0007-local-jwks-verification.md) a bounded decision is
gone before anyone notices, replaced by a service that is unavailable whenever keyring is.

**5. Talking to settings-api stays behind the preferences module.** Nothing but
`persona_api.core.preferences` may import `settings_client`. The container constructs the
source through `build_preference_source`. The client owns caching, revalidation,
single-flight and outage behaviour; a call site that imported it would re-implement those
four, slightly wrong, and would present a user token without the one module that knows how
to degrade.

## What each layer is for

### `domain/` — the rules, as pure functions

`personas.py`, `fields.py`, `notes.py`, `provenance.py`, `secrets.py`, `cursors.py`,
`errors.py`. No I/O, no clock, no configuration.

Three rules live here and each is an invariant:

- **Keys are normalized, not rejected.** `"Favourite Topics"`, `"favourite-topics"` and
  `"FAVOURITE_TOPICS"` all collapse to `favourite_topics`. Key sprawl is fought by making
  reuse easy — normalization, required descriptions, a cheap `/schema` endpoint — and
  never by refusing a write. A refused write means an assistant that has learned something
  and cannot record it.
- **`value_type` is derived from the value, never supplied by the caller**, so the two
  cannot disagree. (And the derivation checks `bool` before `int`, because
  `isinstance(True, int)` is `True` in Python and getting this wrong types every boolean
  as a number, silently.)
- **Credential-shaped input is refused**, with a message naming keyring, and with no
  setting that disables it. [ADR-0002](adr/0002-no-secrets.md)

### `storage/` — rows and transactions, and nothing else

Copied from keyring, with one pragma deliberately changed. It has no idea what a persona
is. Everything that matters about the concurrency design is in the `database.py` module
docstring; the short version is that a single-worker executor makes every transaction
indivisible by construction rather than by convention, which is what every cap and every
revision bump above it depends on.

The differences from keyring are in [ADR-0008](adr/0008-sqlite.md): `synchronous = NORMAL`
rather than `FULL`, and an `fts_sequence` table that hands out full-text docids rather than
borrowing the implicit rowid.

### `events/` — what changed, and when

An append-only log, capped and trimmed **in the same transaction as the insert**, so the
bound is a bound rather than something a sweeper gets round to. No foreign keys, on
purpose: deleting a persona must not delete the record that it was deleted.

Ordered by `sequence`, not by `at`. The clock is injectable and two events recorded in one
tick share a timestamp, so "newest first" has to be insertion order to be an order at all.

**An event never contains a field value or a note body** — only the key or the id. An event
log is read by more people, and kept for longer, than the rows it describes.

### `memory/` — the half the service exists for

`FieldStore`, `NoteStore`, the full-text index, `recall`, the filters and cursor
pagination. Three things here are not obvious:

**The FTS index is written by the store, explicitly, inside the same transaction as the
row**, through one private `_index()` helper called from every write path. Not
external-content tables — those require issuing `'delete'` commands carrying the *old*
values on every update, and missing one silently corrupts the index. Not triggers — those
would have to render a JSON value to searchable text in SQL. The explicit helper creates
its own bug class, which is why index integrity gets its own test class, asserting that the
table and the index agree after create, revise, forget and persona delete.

**Query sanitisation is mandatory.** FTS5 `MATCH` has its own syntax and raw user input
hits it: measured on this stack, eight of ten plausible search strings are a 500.
Word tokens are extracted with `re.findall(r"[^\W_]+", query, re.UNICODE)`, double-quoted,
and joined with ` OR `. A query with no word tokens is a clean 422.

**Cursor pagination, not offset.** The cursor encodes the last `(sort_key, id)` seen, so a
write during a walk cannot make a row appear twice or be skipped. "Retrieve everything"
must work on a persona with five thousand notes, and it is a row-value seek on the partial
index rather than a sort — verified with `EXPLAIN QUERY PLAN`, and there is a test.

### `personas/` — the identity card, and the cascade

`PersonaStore` and `PersonaService`. Small, because the persona itself is small: a name,
pronouns, a sentence. Everything an assistant might want to remember is a field or a note,
because those grow without a migration and this does not.

`GET /v1/personas/{profile}` returns the **identity block** — the card plus *pinned* fields
and notes plus counts. There is deliberately no endpoint returning rendered prose;
turning this into a system-prompt string is the MCP layer's job, and baking a prompt
template into the API would freeze it and put presentation in the wrong service. See
[docs/mcp.md](mcp.md).

### `auth/` — the only package that knows keyring exists

`jwks.py` and `verifier.py`, and both are thin. The rules by which a token is believed —
RS256 only, the issuer pinned, every claim required, expiry on the injected clock — and every
rule about fetching keyring's keys are the family's, in `keyring_client`: shared by every
service, and tested against keyring's own signer in the keyring repository.
[ADR-0007](adr/0007-local-jwks-verification.md) covers the decision.

What is left here is what only this service decides. The audience is **exactly** `persona`,
not a family, because this service has no compartments. And the library's verdicts become
this service's own errors, so nothing above `auth` knows a library was involved: every
refusal is `AuthenticationError` and one identical 401; keys that cannot be fetched, with no
usable copy held, are `KeyringUnreachableError` and a 503.

Two pieces of the library's behaviour are not obvious from the outside. An unknown `kid`
triggers a refetch **rate-limited to one per `jwks_min_refetch_seconds`**, because without the
limit a stream of tokens with random kids is an outbound-fetch amplifier pointed at keyring —
and a `kid` missing from a key set keyring has just served is a 401, not an outage. And keys
from a good fetch are served through a keyring outage for up to a day, which is why
`/ready` can report keyring unreachable while reporting itself ok.

### `api/` — thin on purpose

Routers raise domain errors and let `api/errors.py` decide what that means over HTTP. It is
the only place in the service that maps a failure to a status code, which is what keeps the
handlers thin.

Every `/v1` route takes `CurrentAccountDep` and addresses every store through that account
id. **No route accepts an account id as a parameter**, and no route reads `asserted_by`
from a request body — it comes from the verified token. That is not a rule handlers
remember; it is the only way the dependency makes an account available.

## Isolation, and why it is structural rather than checked

Every store method takes an `account_id`, and it is not optional on any of them. It is part
of the primary key rather than something compared afterwards. There is no call that *could*
read across accounts, so isolation is not a check somebody has to remember to write in a
handler — it cannot be expressed.

The one place this is not automatic is **full-text search**, because the FTS index is
shared across accounts by construction: one `fields_fts`, one `notes_fts`, every account's
text in both. The account filter lives in the `JOIN`, not in the `MATCH`. That works, and
it is exactly the kind of thing that breaks silently, so `TestSearchIsolation` exists
specifically to assert that one account's query never returns another's row.

(`bm25()` also ranks against the whole corpus rather than one account's. At this scale that
is irrelevant to result quality and it leaks nothing — the rows are filtered before they
are returned — but it is worth knowing so nobody later mistakes it for a leak.)

## What this service is not

- **Not multi-process.** One connection, one writer. [ADR-0008](adr/0008-sqlite.md).
- **Not administrable.** No admin API, no roles, no break-glass.
  [ADR-0003](adr/0003-no-administrative-surface.md).
- **Not a vault.** Nothing here is encrypted and credential-shaped input is refused.
  [ADR-0002](adr/0002-no-secrets.md).
- **Not a source of instructions.** [ADR-0001](adr/0001-data-not-instructions.md).
