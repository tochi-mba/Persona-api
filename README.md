# persona-api

An assistant needs somewhere to keep a model of *itself* — what it is called, how it
speaks, what it has learned about the person it serves — and to get that back later.
Today that lives in a prompt somebody retypes, or nowhere. This is the somewhere.

It is the second service in the family that starts with
[keyring](https://github.com/tochi-mba/Keyring-api), and it authenticates against it.

## What a persona is

A small identity card, plus two kinds of knowledge the assistant writes for itself.

**Fields** are structured, named, typed attributes that *the assistant itself defines and
fills*. It invents the key — `voice`, `forms_of_address`, `things_i_got_wrong` — writes a
description saying what the field is for, and sets a JSON value. The schema is not ours;
it grows as the assistant learns what is worth recording about itself.

**Notes** are free text for what does not fit a key: what happened, what was noticed,
what to do differently.

Both are searchable, both carry provenance, and both can be pinned into the identity
block an assistant loads at the start of a turn.

**One persona per profile**, keyed `(account_id, profile)`, mirroring keyring's profiles —
so a "work" assistant and a "home" assistant are different people.

## The line this service does not cross

Four rules, each of which is an ADR and at least one test.

1. **A persona is data, never instructions.** An assistant that reads a web page, an
   email or tool output is reading untrusted text — so a store it writes from that is a
   prompt-injection sink. If a memory can say *"always do X"*, anything the assistant
   ever read can rewrite its behaviour permanently. Every field and note is therefore
   returned **with its provenance attached**, and [docs/mcp.md](docs/mcp.md) instructs
   the MCP layer to render them as third-person reported claims ("your notes say …"),
   never as system instructions. The API cannot enforce that; it can make the honest
   shape the easy one. [ADR-0001](docs/adr/0001-data-not-instructions.md)

2. **No secrets in a persona.** keyring is next door and is the place for them. People
   will paste an API key into "remember this". The API detects credential-shaped input
   and refuses it with a message naming keyring. **There is no setting that disables
   this.** [ADR-0002](docs/adr/0002-no-secrets.md)

3. **No administrative surface.** Every row is reachable only by the account in the
   verified token's `sub`. There is no admin API, no RBAC, no break-glass, and no
   operator endpoint that reads somebody's persona on their behalf. If you want
   somebody's persona, ask them. This is a smaller service than keyring precisely because
   it has nothing to administer.
   [ADR-0003](docs/adr/0003-no-administrative-surface.md)

4. **Provenance is partly a claim, and the API says which part it verified.**
   `asserted_by` is the `aud` of the verified token that made the write — server-derived,
   never read from the request body, trustworthy. `source` is `owner` | `assistant` |
   `service`: who the writer *says* asserted it. The server cannot tell whether a person
   typed it or a model inferred it, so it is labelled as a claim in the schema
   description. [ADR-0004](docs/adr/0004-provenance-is-partly-a-claim.md)

## Getting started

```bash
make install     # create the venv and install everything
make check       # the gate: format, lint, strict types, contracts, 100% branch coverage
make run         # serve on :8002, docs at /docs
```

You will need a running keyring to get a token:

```bash
# in keyring, with KEYRING_SERVICE_TOKENS='{"persona":"..."}' set
curl -sX POST localhost:8001/v1/auth/service-token \
  -H "Authorization: Bearer $SESSION" \
  -d '{"audience":"persona"}'
```

persona-api verifies that token **locally** against keyring's JWKS. It never calls
keyring at request time, and it cannot ask keyring anything about an account — including
whether a profile exists. See [docs/architecture.md](docs/architecture.md).

## The shape of the API

Every endpoint is shaped as a tool call, because that is what it will become: one
operation, one clear name, arguments a model can fill without reading prose. The
`operation_id`s are a public contract, pinned by a test.

| What you want | Call |
| --- | --- |
| The identity block — card, pinned fields, pinned notes, counts | `GET /v1/personas/{profile}` |
| What keys already exist, so you do not invent a fourth name for one thing | `GET /v1/personas/{profile}/schema` |
| Set a field (idempotent — `PUT`, so a retry cannot make two) | `PUT /v1/personas/{profile}/fields/{key}` |
| Write a note | `POST /v1/personas/{profile}/notes` |
| Search this persona | `GET /v1/personas/{profile}/recall?q=…` |
| Search every persona you own | `GET /v1/recall?q=…` |
| Everything, in one response | `GET /v1/personas/{profile}/export` |

Full reference: [docs/api.md](docs/api.md).

## Documentation

| Document | What it covers |
| --- | --- |
| [AGENTS.md](AGENTS.md) | How work is done here. Read before your first edit. |
| [docs/architecture.md](docs/architecture.md) | The layers, the contracts, and why each exists. |
| [docs/api.md](docs/api.md) | Every endpoint, every filter, every failure. |
| [docs/mcp.md](docs/mcp.md) | How to front this with MCP without turning memories into instructions. |
| [docs/operations.md](docs/operations.md) | Deploying, backing up, and the hardening checklist. |
| [docs/testing.md](docs/testing.md) | What each test group defends, and why it exists. |
| [docs/adr/](docs/adr/) | The decisions, and what would change our minds. |

## Licence

MIT. See [LICENSE](LICENSE).
