# AGENTS.md

Working notes for anyone -- human or agent -- changing this codebase. Read this before
your first edit. It is the single source of truth for how work is done here; `CLAUDE.md`
just points at it.

It is adopted wholesale from `tochi-mba/Keyring-api`, which is the sibling service and
the one this borrowed its storage, its logging and its whole idea of a gate from. Where
this file and keyring's differ, the difference is deliberate and says why.

## What this service is

**persona** is one HTTP service. It holds the model an assistant keeps of *itself*: an
identity card, structured **fields** whose keys the assistant invents, and free-text
**notes**. One persona per `(account_id, profile)`, so a "work" assistant and a "home"
assistant are different people.

It is not a library, and it is not part of another service. It authenticates against
keyring by verifying keyring's signed tokens locally; that is the only relationship
between them, and it is one-directional -- persona-api never calls keyring at request
time and cannot ask it anything about an account.

The HTTP surface is designed to be fronted by an **MCP server** later, so an assistant
can call it as tools. That is why route `operation_id`s and descriptions are treated as
contract rather than decoration -- see [Invariants](#invariants).

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything. |
| `make check` | **The gate.** Format check, lint, strict types, layering contracts, tests at 100% branch coverage. Run before every commit. |
| `make matrix` | The tests on every Python CI runs. A green `check` is one interpreter's opinion; coverage genuinely differs between versions. Run before pushing. |
| `make test` | Tests only. |
| `make fmt` | Format and auto-fix. |
| `make run` | Serve on :8004 with reload. Docs at `/docs`. |
| `make cov` | HTML coverage report in `htmlcov/`. |
| `make schema` | Regenerate the checked-in schema snapshot after changing a migration. |
| `make smoke` | End-to-end check against a persona-api already running on :8099. See `scripts/smoke.py`. |

Always run `make check` rather than a bare `pytest` -- piping any of these to `head`/`tail`
in a shell chain masks the exit code, which is how a broken commit slips through.

## The map

```
src/persona_api/
  core/      config, clock, logging, request context, version, preferences, and the composition root
  domain/    pure types and rules: Persona, Field, Note, Source, key normalization,
             value limits, credential refusal. Imports nothing internal.
  storage/   the SQLite connection, the migrations, and how a datetime becomes a column
  events/    the append-only record of persona changes
  memory/    FieldStore, NoteStore, the FTS index, recall, filters, cursor pagination
  personas/  PersonaStore and PersonaService -- the identity card and the cascade
  auth/      token verification: a thin adapter over the family's keyring_client. The only
             package that knows keyring exists
  api/       FastAPI app, routers, wire schemas, problem+json errors, middleware
```

Dependencies point inward:
`api → auth → personas → memory → events → storage → domain`. `core` is a shared kernel
everything may use, except `domain`.

Four contracts, all earned, and a fifth for the second remote:

1. **Layers point inward.**
2. **The domain imports nothing from the rest of the package** -- not even `core`.
3. **SQL stays behind the stores.** `api`, `auth` and `domain` may not import `storage`
   or `sqlite3`. A router that could write a query is a router that will eventually
   contain one.
4. **Talking to keyring stays behind the auth adapter.** Nothing but `auth` may import
   `keyring_client`, `jwt` or `httpx`. This is the one worth explaining: it keeps
   *everything this service asks of keyring, and every rule by which it believes an
   answer*, inside one package that can be read in a sitting. The rules themselves are the
   family's, in `keyring_client` -- change them there, never by re-implementing one here.
   `core.config` is left off that list so it can validate `PERSONA_SETTINGS_API_TOKEN`
   with `keyring_client.check_service_token` rather than a copy of the 32-character rule.
5. **Talking to settings-api stays behind the preferences module.** Nothing but
   `core.preferences` may import `settings_client`. The container constructs the source
   through that module. Reading the client from a call site would re-implement caching,
   revalidation, single-flight and outage behaviour, slightly wrong, and would present a
   user token without the one module that knows how to degrade.

## Invariants

These are enforced mechanically. If you want to break one, change the enforcement
deliberately and say why in the commit message -- do not work around it.

1. **A persona is data, never instructions.** Every field and note is returned with its
   provenance attached, and `docs/mcp.md` instructs the MCP layer to render them as
   third-person reported claims. An assistant that read a web page and wrote what it
   said into a note has created a prompt-injection sink; the API cannot stop that, but
   it can make the honest shape the easy one. [ADR-0001](docs/adr/0001-data-not-instructions.md)
2. **No secrets in a persona.** `domain/secrets.py` refuses credential-shaped input and
   the message names keyring. There is no setting that disables it.
   [ADR-0002](docs/adr/0002-no-secrets.md)
3. **No administrative surface.** Every row is reachable only by the account in the
   verified token's `sub`. No admin API, no RBAC, no break-glass, no operator endpoint
   that reads somebody's persona on their behalf. If you want somebody's persona, ask
   them. [ADR-0003](docs/adr/0003-no-administrative-surface.md)
4. **`asserted_by` is server-derived and `source` is a claim**, and the schema says which
   is which. `asserted_by` comes from the verified token's `aud` and a request body
   claiming otherwise is ignored. [ADR-0004](docs/adr/0004-provenance-is-partly-a-claim.md)
5. **Cross-account access is 404, never 403.** A 403 confirms the row exists.
6. **Nothing reads the wall clock directly.** Every component that behaves differently
   over time takes a `Clock`. This includes JWT expiry -- `keyring_client` turns PyJWT's
   own time checks off and judges expiry against the clock this service injects, because
   otherwise a test could only ever assert that a token minted now is valid now.
7. **The algorithm list is fixed.** `algorithms=["RS256"]`, pinned in `keyring_client` and
   never read from the token. `alg: none` and an HS256 token signed with the JWKS public
   key are both refused, and this service's own suite has a test for each.
8. **Every search query is sanitised before it reaches `MATCH`.** FTS5 has its own
   syntax and raw user input hits it: eight of ten plausible search strings are a 500
   without this. Word tokens are extracted, quoted, and joined with ` OR `.
9. **The FTS index is written by the store, in the same transaction as the row**, through
   one private `_index()` helper. Not external-content tables, not triggers -- see
   `memory/search.py`. A test asserts the table and the index agree after every operation.
10. **Caps are enforced inside the transaction that does the write.** A caller that counts
    and then writes has a window in between, and there is an `asyncio.gather` test per cap
    that would find it.
11. **Coverage is 100% branch coverage, and the exclusions are only non-executable lines**
    -- `if TYPE_CHECKING:`, bare `...` protocol bodies, `@overload`, the `__main__` guard.
    There is no `# pragma: no cover` in `src/`. If a line is hard to cover, that is
    usually the code telling you it is shaped wrong.
12. **Route `operation_id`s are public API.** They become MCP tool names. A contract test
    pins the exact set and requires a summary and a real description on every operation.
13. **A field's `value_type` is derived from its value, never supplied by the caller**, so
    the two cannot disagree.
14. **Field keys are normalized, not rejected.** `"Favourite Topics"`, `"favourite-topics"`
    and `"FAVOURITE_TOPICS"` are one field. Key sprawl is fought by making reuse easy --
    required descriptions, a cheap `/schema` endpoint -- and never by refusing a write.

## How we work: TDD

Every change follows red → green → refactor, and each commit leaves `make check` passing.

1. Write the test first. It should fail for the reason you expect -- check that it does.
2. Write the smallest implementation that passes.
3. Refactor with the test as a safety net.

Conventions, inherited and worth repeating:

- **Name tests after the behaviour**, in full sentences.
  `test_one_accounts_search_never_returns_anothers_note` beats `test_search_isolation`.
- **Write the "why" when it is not obvious.** A comment explaining that the entropy
  heuristic is deliberately conservative because a false positive blocks a legitimate
  memory and there is no override is worth more than the assertion.
- **Don't assert an object is truthy.** `assert await store.get(...)` always passes.
  Strict mypy's `truthy-bool` catches it.
- **Fakes are hand-written and must satisfy the real Protocol** (`tests/fakes/`). If a
  port changes they fail to type-check, which is how you find out.
- **Never sleep in a test.** Inject the clock. Token expiry and the JWKS cache window are
  both defined by time and neither needs a real second to test.
- **Assert the outcome, never the mechanism.** The concurrency tests say "one row, one
  revision bump, no lost write", not "the lock was held".

## Recipe: add an endpoint

1. Add wire models in `src/persona_api/api/schemas/<name>.py`. Set
   `model_config = ConfigDict(extra="forbid")` on request bodies so an invented field is
   rejected rather than ignored. Give every field a `description` and every model an
   `examples` entry -- a model reads these to decide whether and how to call the tool.
2. On the route set `operation_id` (snake_case `verb_noun`, stable forever), `summary`, a
   real `description`, and `responses` for every failure a caller can provoke.
3. Register the router in `ROUTERS` in `api/routers/__init__.py`. That is the only wiring
   step; problem+json, request ids, the account binding and access logging are inherited.
4. Take `CurrentAccountDep` and address every store through that account id. **Never
   accept an account id as a parameter**, and never read `asserted_by` from a body.
5. Raise domain errors. Map any new one in `_DOMAIN_STATUS` in `api/errors.py` -- never
   build an error response in a handler.
6. Tests: an integration test per behaviour, an isolation test per verb, and extend the
   OpenAPI contract test with the new `operation_id`.

## Recipe: add a field limit

1. Add the setting to `Settings` in `core/config.py`, with a default.
2. Enforce it in `domain/values.py`, and make the message **name which limit failed** --
   a caller told only "invalid" has to bisect its own payload.
3. Add it to `.env.example` and to `docs/operations.md`.
4. Tests: the value just under, the value just over, and the message naming the limit.

## Environment gotchas

- `asyncio_mode = "auto"`, so `async def test_*` needs no marker.
- `filterwarnings = ["error"]`: a new deprecation warning fails the suite. Fix it rather
  than filtering it. Starlette's `HTTP_422_UNPROCESSABLE_ENTITY` is deprecated in favour
  of `HTTP_422_UNPROCESSABLE_CONTENT`, and using the old name will fail the suite.
- A `PERSONA_`-prefixed variable that no setting matches is a **startup error**.
- `synchronous = NORMAL`, unlike keyring's `FULL`. A lost persona write is a forgotten
  note; a lost keyring write is a credential somebody believes is saved. See
  [ADR-0008](docs/adr/0008-sqlite.md).
- The tests mint real RS256 tokens and serve the JWKS through `keyring_client.testing`, the
  family's shared fake, by way of `tests/fakes/keyring.py`: a real key, an
  `httpx.MockTransport`, no network in the suite and no `unittest.mock`.
- `keyring-client` is a path dependency on `../Keyring-api/clients/python`, so `make install`
  needs the keyring repository checked out beside this one.
- `settings-client` is a path dependency on `../Settings-api/clients/python`, for the same
  reason. Unset `PERSONA_SETTINGS_API_BASE_URL` keeps today's behaviour; the client is
  imported either way.

## Commit conventions

Conventional-commit subject (`feat(scope):`, `fix(scope):`, `chore:`), imperative mood, no
trailing period. The body explains **why** -- the tradeoff, the failure mode being
prevented, the thing that surprised you. A reader six months from now has the diff
already; what they lack is your reasoning.

## Definition of done

- [ ] Tests were written first, and failed first.
- [ ] `make check` passes: format, lint, strict types, layering contracts, 100% coverage.
- [ ] `make matrix` passes before pushing.
- [ ] New behaviour is covered by a test named after the behaviour.
- [ ] Anything reading or writing a persona has an **isolation test** proving another
      account gets a 404 identical to the one a nonexistent persona gets.
- [ ] Anything that writes records an event, and the event contains no field value and no
      note body -- only the key or the id.
- [ ] Anything that stores caller text goes through `looks_like_a_credential` first.
- [ ] Anything that touches a write path keeps the FTS index in the same transaction, and
      the index-integrity test covers the new path.
- [ ] Public HTTP changes: `operation_id`s stable, descriptions written for a model to
      read, contract test updated.
- [ ] Docs updated -- this file for workflow, `docs/` for design, an ADR for a decision
      that future-you would otherwise re-litigate.
