# ADR-0008: SQLite, one file, and why `synchronous` differs from keyring's

**Status:** accepted. Adopts
[keyring's ADR-0012](https://github.com/tochi-mba/Keyring-api/blob/main/docs/adr/0012-sqlite.md)
with one deliberate difference.

## Context

keyring settled this question already: one SQLite database, WAL mode, the stdlib `sqlite3`
driver, a single connection owned by a single-worker `ThreadPoolExecutor`, hand-rolled
numbered migrations. The reasoning is in its ADR-0012 and is not re-derived here.

This service has the same shape — one process, a handful of people, no availability
requirement — so it inherits the decision rather than re-litigating it.

## What was copied unchanged, and why each matters

**A single-worker `ThreadPoolExecutor`, not `asyncio.to_thread` under an `asyncio.Lock`.**
Cancelling a task that awaits `to_thread` unwinds the `async with` and releases the lock
but **does not cancel the thread**, so the next caller enters the same connection
mid-transaction. A client disconnect cancels a request task, so this is an ordinary
Tuesday rather than a thought experiment. One thread removes the failure instead of
patching it, and makes every transaction indivisible by construction.

**`PRAGMA foreign_keys` is read back and verified.** It defaults off, is per-connection,
and is a silent no-op while a transaction is open. Here the consequence would be that
deleting a persona leaves its fields and notes behind — orphaned rows nothing can reach,
read or delete, holding exactly the text somebody asked to be rid of.

**`make_private`.** SQLite creates files 0644. In keyring that is defence in depth, because
the credential material is encrypted anyway. **Here it is the whole defence**: nothing in
this file is encrypted, and a persona is a person's own words about themselves. 0600 on the
database and on its `-wal`/`-shm` sidecars is the only thing between that and every other
process on the box.

**`STRICT` on every table.** Without it SQLite stores a string in an INTEGER column
happily, and a row-mapper bug becomes data that reads back as the wrong type months later
rather than a failure at the write.

## The one difference: `synchronous = NORMAL`, not `FULL`

keyring uses `FULL` because a lost transaction there is a credential somebody believes is
saved and is not — discovered later, by something failing to authenticate, with no way to
tell what went.

Here a lost transaction is a **forgotten note**. Annoying, recoverable by writing it again,
and visible immediately to the assistant that wrote it. The write volume also differs by
orders of magnitude: keyring writes a few times a minute, and an assistant with a persona
may write on every turn.

WAL with `NORMAL` **cannot corrupt the database**. It can only lose the last commits on
power loss, because the WAL is still fsynced at every checkpoint. Trading that against an
fsync per commit is the right way round for this service, and the wrong way round for
keyring — which is why the two differ, and why the difference is written here rather than
left for somebody to "fix" into consistency.

## The FTS docid allocator, which is ours rather than keyring's

Both full-text indexes are keyed by an integer handed out by an `fts_sequence` table,
rather than by the implicit rowid of the row being indexed. Two reasons:

- **`VACUUM` renumbers the implicit rowids of a table with no INTEGER PRIMARY KEY**, and
  `VACUUM INTO` is what [docs/operations.md](../operations.md) tells an operator to back up
  with. An index joined on an implicit rowid would come back from that backup pointing at
  the wrong rows — silently, with every search returning somebody else's text.
- **A counter that only goes up cannot reuse a docid freed by a delete**, so a stale index
  entry can never be mistaken for a live one.

## The indexes, and the one that was removed

Three partial indexes per table, each excluding forgotten rows, each carrying the sort
columns as well as the filter columns so it serves the `ORDER BY` too. Verified with
`EXPLAIN QUERY PLAN`, and there is a test that keeps verifying it.

A full index on `notes(account_id, profile, created_at DESC, note_id DESC)` was added first
for the `include_forgotten` path and was a **mistake**: with no `ANALYZE` statistics the
planner cannot tell a partial index from a full one with the same prefix, so it chose the
full one for *every* query and the three partial indexes became dead weight. Nobody would
ever have noticed without running `EXPLAIN QUERY PLAN`.

What replaced it narrows but does not sort — `notes(account_id, profile)` — so it cannot
shadow the partial indexes, and the rare listing that wants forgotten rows still reads one
persona's notes rather than scanning every account's.

## What would change our minds

The same thing that would change keyring's: more than one process needing to write. That is
a Postgres adapter, a different answer for the FTS index (`tsvector`), and a different
`synchronous` conversation — and the stores are shaped so it would inherit this test suite
rather than need a new one.
