# ADR-0009: Erasure is the person's choice, written on the row; `@default` names their default persona

**Status:** accepted.

## Context

settings-api's catalogue carries four `persona` settings that persona-api did not read,
each marked *proposed* because the mechanism behind it did not exist here:

- `persona.erasure_mode` (`grace` | `immediate` | `tombstone`) and `persona.grace_days` —
  what forgetting one field or note does. persona-api had one answer, never named: a
  tombstone that nothing ever purged, and no sweeper.
- `persona.log_values` — whether the change log keeps the value a change replaced. The log
  kept no values at all.
- `persona.default_persona` — which persona to load when a conversation does not name
  one. Every route takes the persona as a path segment, and there was no default.

user-api already implements the first three, spelled the same way on purpose: a person who
has decided what "delete" means should not have to answer a differently-shaped question
for the next service. Its sweeper reads each account's settings from its own store,
because a sweeper has no user token to present to settings-api — which is why user-api
keeps those three settings out of settings-api for now.

The rule for all four: **a person who has chosen nothing gets exactly what persona-api
did before.**

## Decision

### Erasure: decided when something is forgotten, and written on the row

`forget_field` and `forget_note` read the caller's `erasure_mode` and `grace_days` from
settings-api — the request carries the person's token, so that is the one moment the
question can be asked — and write the answer onto the row:

| Mode | What the request does |
| --- | --- |
| `tombstone` | Sets `forgotten_at`, as before. `purge_after` stays `NULL`: never. |
| `grace` | Sets `forgotten_at`, and `purge_after = now + grace_days`. Setting a field again before then clears both. |
| `immediate` | Deletes the row, its search entry and the values the log kept about it, in the same transaction; then truncates the write-ahead log. |

A sweeper then destroys rows whose `purge_after` has passed. It reads no settings and
presents no token: whether and when is already a fact about the row. That is the
difference from user-api, and it buys three things:

1. **The sweeper needs no token**, so these settings can live in settings-api, where the
   catalogue puts them, without a second store and a dual write.
2. **A change is never retroactive, by construction.** Nothing re-reads the setting and
   applies it to old rows. Switching to `immediate` leaves what is already waiting out a
   grace period; leaving `tombstone` schedules nothing already tombstoned.
3. **The sweep is one indexed query across every account** — `purge_after <= now`, served
   by a partial index over only the waiting rows — instead of a settings lookup per
   account.

The sweeper is **bounded**: at most 500 fields and 500 notes per sweep, oldest-due first,
so a backlog is worked off over several sweeps rather than holding the single database
thread. It sweeps at startup and then every `PERSONA_PURGE_INTERVAL_SECONDS` (an hour), so
a grace period ends up to one interval late and never early. A failed sweep is logged and
retried next tick, never ends the task.

**Destroyed means the bytes are gone.** user-api measured that a `DELETE` alone leaves the
text in the page's free space and in the `-wal` file. persona-api now runs with
`PRAGMA secure_delete = ON` and truncates the write-ahead log once after every immediate
erasure, sweep that destroyed anything, persona delete, and write that destroyed a field
whose grace period had ended before the sweep reached it.

persona-api has a third copy user-api's measurement did not cover: the full-text index. An
FTS5 `DELETE` writes a delete marker and leaves the document's words — lowercased and
stemmed, but readable with `grep` — in the older segments until a merge reaches them. So
every transaction that destroys something also runs the index's `optimize` command, which
merges it into one segment without them. FTS5's `secure-delete` option would do it per
delete and more cheaply, but needs SQLite 3.42, and the Debian bookworm image ships 3.40.1.
`optimize` rewrites the whole index, so it runs once per destroying transaction, never per
row. Tests scan the database and its `-wal` for both the sentinel text and its indexed word
after an immediate erasure, a sweep, a destroying write and a persona delete.

### Logged values: kept only when asked, stripped with their row

With `log_values` on, a change that **replaces a field's value or a note's body** records
what it replaced, as JSON, in a new `events.old_value` column, returned as
`old_value: {"value": ...}`. A change to a pin, a kind or a description replaced nothing
the person asked to have kept and records nothing. `detail` never holds a value.

Whatever destroys a row strips `old_value` from every event about it **in the same
transaction**: the sweeper, an immediate erasure, and deleting the persona. A field key and
a note id share the `subject` column, so a strip is scoped by noun (`field.%` / `note.%`)
as well as by subject. Under `tombstone` nothing is destroyed, so the copies live exactly as
long as the row they copy.

### `@default`: a reserved path segment

Anywhere a path takes `{profile}`, the segment `@default` is resolved to the person's
`default_persona` before the route runs, through one FastAPI dependency every such route
takes. `@` is outside the profile-name rule (keyring's own), so no persona can ever be
called that and no existing path changes meaning.

With no default chosen, the segment is handed on unchanged and fails exactly as it always
has — a 422 naming the profile rule. The catalogue's reasoning for a null default applies
here too: loading *a* persona the person did not choose is worse than failing loudly. The
same is true during an outage with nothing cached.

The default is asked for **with no profile**, because which persona to load is asked
before there is one to name. Then the person's other settings are read for the profile it
resolved to, so `@default` gets that persona's own page size.

## Alternatives considered

- **A resolution endpoint** (`GET /v1/default-persona` returning a profile). One more MCP
  tool, one more round trip on every conversation start, and a race between the two calls.
  `/v1/personas/default` was never available: `default` is a valid profile name.
- **user-api's design for erasure** (per-account settings read by the sweeper). Needs the
  settings in persona-api's own store, because the sweeper has no token; that means a
  second home for a setting the catalogue already has, and either a dual write or settings
  that never reach settings-api.
- **Re-reading the mode at sweep time.** Makes every settings change retroactive, which
  the catalogue forbids in as many words.

## Consequences

- The catalogue default for `persona.erasure_mode` has to be `tombstone`, not `grace`, for
  "nobody who chose nothing sees a change" to hold. settings-api answers every unchosen key
  with its catalogue default, and persona-api cannot tell an unchosen `grace` from a chosen
  one. Until that default changes, turning settings-api on schedules the destruction of
  everything anybody forgets, thirty days out.
- `persona.default_persona` has to be **account**-scoped. It is catalogued per profile; read
  without a profile, a profile-scoped key always answers its default (null), so `@default`
  would never resolve. Choosing a default persona *per persona* is circular, for the same
  reason settings-api already refuses a per-profile `common.default_profile`.
- `forget_field` and `forget_note` now read settings, so with settings-api on, a 401/403
  from it makes them 503, as it already did every route that reads a pin cap.
- Backups taken before a row was destroyed still hold it. That is the operator's to manage,
  and `docs/operations.md` says so.
- Destroying anything rewrites the search index it was in: `optimize` costs time in
  proportion to the whole index, not to what was destroyed, on the single database thread.
  A sweep pays it at most once per table, an immediate erasure once. When the image moves
  to a SQLite with FTS5 `secure-delete`, that option should replace it.

## What would change our minds

A need to destroy something sooner than the person's own setting says — which would be an
administrative surface, and [ADR-0003](0003-no-administrative-surface.md) is the answer to
that. Or a family-wide move of user-api's erasure settings into settings-api, in which case
user-api should adopt this design rather than the other way round.
