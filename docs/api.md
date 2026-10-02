# The HTTP API

Twenty-one operations. Every one is shaped as a tool call, because that is what it will
become — one operation, one clear name, arguments a model can fill without reading prose.

`operation_id`s are a **public contract**: they become MCP tool names, so renaming one
breaks every client with a tool bound to it. A test pins the exact set.

Interactive docs at `/docs` on a running instance; the machine-readable contract is
`/openapi.json`.

## Authenticating

Every route except `GET /healthy` and `GET /ready` needs a keyring token:

```
Authorization: Bearer <RS256 JWT>
```

Mint one from keyring:

```bash
curl -sX POST "$KEYRING/v1/auth/service-token" \
  -H "Authorization: Bearer $SESSION" \
  -H "Content-Type: application/json" \
  -d '{"audience": "persona"}'
```

persona-api verifies it **locally** against keyring's JWKS — it never calls keyring at
request time. The token's `sub` is the only identity this service gets; its `aud` becomes
`asserted_by` on everything you write, and must be exactly this deployment's
`PERSONA_AUDIENCE` — `persona` unless the operator changed it. See
[ADR-0007](adr/0007-local-jwks-verification.md).

Two failures worth telling apart:

| | Meaning |
| --- | --- |
| **401** | The token was not accepted. One message for every reason — expired, wrong audience, wrong issuer, forged, malformed, or signed with a key keyring does not publish. You learn nothing from which, deliberately. |
| **503** | keyring could not be reached to fetch the verifying keys. **Your token is probably fine.** Retry shortly; do not re-authenticate. |

## The operations

| Method | Path | operation_id |
| --- | --- | --- |
| GET | `/healthy` | `get_health` |
| GET | `/ready` | `check_readiness` |
| GET | `/v1/personas` | `list_personas` |
| POST | `/v1/personas` | `create_persona` |
| GET | `/v1/personas/{profile}` | `get_persona` |
| PATCH | `/v1/personas/{profile}` | `update_persona` |
| DELETE | `/v1/personas/{profile}` | `delete_persona` |
| GET | `/v1/personas/{profile}/schema` | `describe_persona_schema` |
| GET | `/v1/personas/{profile}/fields` | `list_fields` |
| PUT | `/v1/personas/{profile}/fields/{key}` | `set_field` |
| GET | `/v1/personas/{profile}/fields/{key}` | `get_field` |
| DELETE | `/v1/personas/{profile}/fields/{key}` | `forget_field` |
| GET | `/v1/personas/{profile}/notes` | `list_notes` |
| POST | `/v1/personas/{profile}/notes` | `write_note` |
| GET | `/v1/personas/{profile}/notes/{note_id}` | `get_note` |
| PATCH | `/v1/personas/{profile}/notes/{note_id}` | `revise_note` |
| DELETE | `/v1/personas/{profile}/notes/{note_id}` | `forget_note` |
| GET | `/v1/personas/{profile}/recall` | `recall` |
| GET | `/v1/recall` | `recall_everywhere` |
| GET | `/v1/personas/{profile}/export` | `export_persona` |
| GET | `/v1/personas/{profile}/events` | `read_persona_events` |

## `@default`: the persona nobody named

Anywhere a path takes `{profile}`, `@default` may stand in for it: it means the persona the
person chose as their default in settings-api (`persona.default_persona`). So
`GET /v1/personas/@default` is the identity block of whichever persona they load when a
conversation does not say, and `PUT /v1/personas/@default/fields/voice` writes to it.
Responses name the real profile.

`@` is outside the profile-name rule, so no persona can ever be called `@default` and no
existing path changes meaning. With no default chosen — or settings-api not configured, or
unreachable with nothing cached — `@default` is refused exactly as any unstorable profile
name always was: a **422** naming the profile rule. That is deliberate. Loading *a* persona
the person did not choose would be worse than failing loudly, because a persona is the
voice the assistant speaks in. There is no new operation for this; it is one more value
the existing `profile` parameter accepts.
[ADR-0009](adr/0009-erasure-and-the-default-persona.md)

## Personas

A persona is one identity card per `(account, profile)`. **Writing a field or a note
creates the persona if there is none**, so `create_persona` is only needed when you want
to set the card up front.

`GET /v1/personas/{profile}` is the **identity block** — the card, the *pinned* fields and
notes, and live counts of both:

```json
{
  "persona": {"profile": "work", "display_name": "Ada", "pronouns": "they/them",
              "summary": "Dry, concise, no preamble.",
              "created_at": "2026-01-04T09:12:00Z", "updated_at": "2026-03-02T17:40:00Z"},
  "fields": [ /* pinned only */ ],
  "notes":  [ /* pinned only */ ],
  "field_count": 24,
  "note_count": 311
}
```

The counts are there so you can tell there is more to ask for. There is **no endpoint that
returns rendered prose** — turning this into a system prompt is the caller's job, and
[docs/mcp.md](mcp.md) says how to do it without turning memories into instructions.

`PATCH` changes only the keys you send. Sending `"pronouns": null` clears it; omitting
`pronouns` leaves it alone. Those are different requests.

`DELETE` is **a hard delete whatever anybody's erasure setting says**, and it cascades:
every field, every note, every index entry, and any old values the change log kept about
them. No undo, and no operator who can recover it — this service has no administrative
surface ([ADR-0003](adr/0003-no-administrative-surface.md)). To remove one memory, forget
the field or note instead; that is reversible unless the person chose immediate erasure.

A profile is validated for **shape only**. persona-api cannot check it against keyring —
there is no endpoint that would answer — so `wrok` creates a second empty persona rather
than failing. `list_personas` is how you notice.
[ADR-0005](adr/0005-one-persona-per-profile.md)

## Fields

Named, typed attributes whose keys you invent.

```http
PUT /v1/personas/work/fields/forms_of_address
{
  "description": "What to call them, and what never to call them.",
  "value": ["Alex", "never 'Alexander'"],
  "source": "owner",
  "pinned": true
}
```

- **`PUT`, so it is idempotent.** A retry cannot create two fields, and writing the same
  value again does not bump `revision`.
- **The key is normalized to snake_case.** `Favourite Topics`, `favourite-topics` and
  `FAVOURITE_TOPICS` are one field.
- **`description` is required.** It is what lets a later write reuse this key instead of
  inventing a synonym — call `describe_persona_schema` first to see what exists.
- **`value_type` is derived by the server**, never sent, so the two cannot disagree.
- Values may be a scalar, a list of scalars, or a one-level object. Limits: 4096 bytes
  serialized, depth 3, 100 list items, 50 object keys. A refusal **names which limit**.

`GET .../schema` returns keys, descriptions, types and `updated_at` — **with no values**,
so it is cheap enough to call before every write. That is the anti-sprawl mechanism.

`DELETE` on a field **forgets** it: it stops appearing in reads and in search at once.
What happens next is the person's choice, not the caller's — see
[Forgetting](#forgetting). By default it is kept and returns with
`?include_forgotten=true`, and setting the key again revives it.

## Notes

Free text for what does not fit a key.

```http
POST /v1/personas/work/notes
{"body": "Ask before refactoring across more than one file.", "kind": "lesson"}
```

`kind` is `episode` (something happened), `observation` (something noticed) or `lesson`
(something to do differently) — a closed set, so a typo cannot make a note unfindable.

Notes **accrue**; they do not replace. Two notes saying the same thing at different times
are two notes, which is the difference between a note and a field.

`PATCH` changes only the parts you name, so you can pin a note without resending its body.
Editing a note does **not** move it in time — it stays where it was in the timeline.

`DELETE` on a note forgets it, as a field's does. A forgotten note cannot be revised back.

## Forgetting

What `forget_field` and `forget_note` do is **the person's** setting in settings-api,
`persona.erasure_mode`, spelled exactly as user-api's `user.erasure_mode`:

| `erasure_mode` | What forgetting does |
| --- | --- |
| `tombstone` | Hidden, and kept for ever. `?include_forgotten=true` brings it back; setting a field's key again revives it. **What everybody gets until they choose otherwise, and what every deployment without settings-api does.** |
| `grace` | Hidden now, destroyed after `persona.grace_days` (30 unless chosen, 0 to 365). Until then it behaves as a tombstone, and the forgotten row carries `purge_after`, the instant it goes. Setting a field again before then revives it and calls the destruction off; setting it after then starts a new field, even if the sweep has not yet run. |
| `immediate` | Destroyed before the `204` is sent: the row, its search entry, and any old values the change log kept about it. No undo. |

The choice is taken **when something is forgotten**, and written on the row. Changing the
setting later never reaches back: switching to `immediate` does not destroy what is
already waiting out a grace period, and leaving `tombstone` does not schedule what is
already tombstoned. A sweeper destroys what is due, once an hour by default
(`PERSONA_PURGE_INTERVAL_SECONDS`), at most 500 fields and 500 notes per sweep.

The forget event's `detail` says which happened: empty for a tombstone (as it always was),
`erased after <instant>` for a grace period, `erased` for immediate.
[ADR-0009](adr/0009-erasure-and-the-default-persona.md)

## Provenance — on every field and every note

| Field | Trust |
| --- | --- |
| `asserted_by` | **Server-derived.** The `aud` of the verified token. Cannot be set from a request body — trying is a 422, not a silent ignore. |
| `source` | **A claim.** `owner` \| `assistant` \| `service`: who the writer *says* asserted it. Nothing verifies it and nothing could. |
| `revision` | How many times the value actually changed. Rewriting the same value does not count. |

Both come back on every read, never as an optional expansion. A memory separated from its
provenance has lost the thing that lets a reader discount it.
[ADR-0004](adr/0004-provenance-is-partly-a-claim.md)

## Filters

Every list endpoint accepts these, and they combine. All are index-backed.

| | Fields | Notes |
| --- | --- | --- |
| `q` | full text over key, description and value | full text over the body |
| `source` | ✓ | ✓ |
| `pinned` | ✓ | ✓ |
| `kind` | — | `episode` \| `observation` \| `lesson` |
| `key_prefix` | `forms_` finds every key under it | — |
| `keys` | `keys=voice,tone` — several named fields in one call | — |
| `since` / `until` | on **`updated_at`** | on **`created_at`** |
| `include_forgotten` | ✓ | ✓ |
| `limit` | default 20, max 100 | default 20, max 100 |
| `cursor` | ✓ | ✓ |

The default page is `PERSONA_RECALL_DEFAULT_LIMIT` (20), or a lower
`persona.recall_default_limit` the person chose in settings-api for the profile in the path
when that is configured. `recall_everywhere` names no profile, so no one profile's choice
applies to it.
The maximum is `PERSONA_RECALL_MAX_LIMIT` (100); asking for more is a 422, never a quietly
shorter page.

**`since`/`until` name different columns on purpose.** A field is current state, so you ask
when it last changed. A note is something that happened, so you ask when it happened — and
editing an old note does not make it today's news. Each list is ordered by the same column
it filters on, which is what keeps both index-backed.

`?q=` makes the response **ranked rather than ordered**, so it comes back without a cursor:
a cursor over `bm25` would mean re-ranking the whole corpus for every page.

## Pagination

Keyset, not offset. `next_cursor` encodes the last row seen, so a write during a walk
cannot make a row appear twice or be skipped:

```bash
GET /v1/personas/work/notes?limit=50
GET /v1/personas/work/notes?limit=50&cursor=MjAyNi0wMy0wMi4uLg
```

`next_cursor` is `null` on the last page — including a *full* last page, so you never make
one extra request for nothing. A cursor this service did not issue is a 422.

## Search

`GET /v1/personas/{profile}/recall?q=…` searches one persona;
`GET /v1/recall?q=…` searches every persona you own, and each hit says which profile it
came from.

```json
{"fields": [ /* ranked within the field index */ ],
 "notes":  [ /* ranked within the note index  */ ]}
```

**Two lists, not one merged ranking.** Merging would compare `bm25` scores computed over
two different corpora, which is not meaningful — it would look tidier and quietly mis-rank.
It also tells you which kind of thing you are reading.

Stemmed, so `preferring` finds `prefer`. Keyword search, not embeddings — and
[ADR-0006](adr/0006-keyword-search.md) says what would change our minds.

Your query is tokenised before it reaches the search engine: operators you type become
words to search for. `answers"`, `concise OR`, `NEAR(` and `*` are all fine. A query with
no words in it is a 422, because "no matches" and "you did not ask for anything" are
different answers.

## Export and events

`GET .../export` returns the card, every live field and every live note in one response
(with `?include_forgotten=true`, the forgotten ones too).
The two halves page **independently** (`field_cursor`, `note_cursor`), because a persona
can be long in fields and short in notes.

`GET .../events` returns the change log, newest first. It records what was set, revised or
forgotten, by whom and when — by default **never a field value or a note body**, only the
key or the id, so it holds as little as possible.

A person may turn `persona.log_values` on in settings-api. Then a change that replaces a
field's value or a note's body keeps what it replaced, in `old_value`:

```json
{"action": "field.revised", "subject": "voice", "detail": "revision 3",
 "old_value": {"value": "dry and concise"}, "...": "..."}
```

`old_value` is `null` for every other event, for every change made while the setting was
off, and — the half that keeps the promise — once the field or note it describes is
destroyed by a grace period ending, an immediate erasure or a persona delete, which strip
it in the same transaction. Under `tombstone` nothing is destroyed, so nothing is stripped.

## Errors

Every failure is RFC 9457 problem+json:

```json
{
  "type": "https://persona.invalid/problems/validation-failed",
  "title": "Validation failed",
  "status": 422,
  "detail": "this looks like a credential (it starts with 'AKIA', the AWS access key id prefix); store it in keyring instead; this service refuses credentials",
  "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b"
}
```

| Status | When |
| --- | --- |
| 401 | The token was not accepted. Same body every time. |
| 404 | No such persona, field or note — **identical** to the answer for one belonging to another account. There is no 403 in this service. |
| 409 | You already have a persona for that profile. |
| 422 | A limit was broken (the message names which), a value looked like a credential (the message names keyring), a cursor was not ours, a search had no words in it, a `limit` was above the maximum, or the request itself was malformed — an invented body field, a bad profile or a bad query parameter. |
| 429 | A cap: personas per account, fields or notes per persona, or pinned entries. |
| 500 | A bug. The detail is withheld deliberately — quote the `request_id`. |
| 503 | keyring is unreachable, so the token could not be checked — not your token. Or settings-api refused this service, which is a deployment misconfiguration; the body is fixed text either way. A route that reads the person's settings — the pin caps, the default page, what forgetting does, `@default` — answers this rather than guessing. |

Request bodies are `extra="forbid"`: an invented field is a 422, not a silent ignore.
That is what stops an `asserted_by` in a body from looking like it worked.

## Caps

| | Default | Setting |
| --- | --- | --- |
| Personas per account | 20 | `PERSONA_MAX_PERSONAS_PER_ACCOUNT` |
| Fields per persona | 500 | `PERSONA_MAX_FIELDS_PER_PERSONA` |
| Notes per persona | 5000 | `PERSONA_MAX_NOTES_PER_PERSONA` |
| Pinned fields / notes | 20 each | `PERSONA_MAX_PINNED_FIELDS` / `_NOTES` |
| Field value | 4096 bytes | `PERSONA_MAX_FIELD_VALUE_BYTES` |
| Note body | 4000 chars | `PERSONA_MAX_NOTE_BODY_CHARS` |

**Pinned is a token budget, not a preference** — every pinned entry goes into the
assistant's prompt on every turn. When settings-api is configured, a person may set lower
pin caps for themselves (`persona.max_pinned_fields`, `persona.max_pinned_notes`); nobody
can raise them above these. Forgetting a row frees its slot; the caps count live
rows.
