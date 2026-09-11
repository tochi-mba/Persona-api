# ADR-0006: Keyword search, not embeddings

**Status:** accepted.

## Context

"Easily retrieve whatever we want" is the requirement this service exists for. The modern
reflex is vector search: embed every note, embed the query, return nearest neighbours.
Semantic recall finds "I like my answers short" when you search for "brevity", and keyword
search does not.

## Decision

**SQLite FTS5**, with `porter unicode61` stemming and `bm25()` ranking. No embeddings, no
vector index, no second store.

Two plain FTS5 tables — `fields_fts` and `notes_fts` — maintained by the stores inside the
same transaction as the row they index.

## Why

**An embedding store is three dependencies wearing one name.** It needs a model to produce
vectors (either an API call on every write and every query, or a local model in the
process), an index that can search them, and a second copy of the data to keep in sync
with the first. Each is a thing to run, back up, version and be woken up by. FTS5 is
already in the SQLite that is already in the standard library — verified on this stack,
with stemming and ranking — and adds nothing to `pyproject.toml`.

**The corpus is small and the queries are specific.** A persona is a few hundred fields and
a few thousand notes written by one assistant about one person, in that person's and that
assistant's own vocabulary. That is close to the best case for keyword search and close to
the worst case for the *added value* of semantic search: the words in the query are usually
the words in the note, because the same model wrote both.

**Stemming covers most of the gap cheaply.** `porter` means a search for "preferring"
finds "prefer" — which is the majority of the near-misses anyone actually hits.

**The structured half does not need it at all.** Fields are looked up by key, by prefix,
and by the `?keys=` batch fetch. Semantic search on `voice` would be a worse way to get
`voice`.

## What it costs, stated plainly

Recall genuinely fails on paraphrase. Search "brevity" and a note saying "keep it short"
will not come back. The mitigations are real but partial: the field half is
key-addressable, `/schema` lets an assistant see what keys exist before inventing a
synonym, and pinning puts the things that matter most into the identity block where they
are never searched for at all.

## What we also decided along the way

**Recall returns two lists, not one merged ranking.** `fields` and `notes` are each ranked
by `bm25()` **within their own index**. Merging them would mean comparing bm25 scores
computed over two different corpora, which is not a meaningful comparison — it would look
tidier and quietly mis-rank. Two sections is also more useful to a model: structured
attributes and narrative notes are different things and it should know which it is reading.

**Every query is sanitised before it reaches `MATCH`.** FTS5 has its own query syntax and
raw user input hits it. Measured on this exact stack, eight of ten plausible search strings
raise `OperationalError` — `'answers"'`, `'concise OR'`, `'NEAR('`, `'*'`, `''`. Word
tokens are extracted with `re.findall(r"[^\W_]+", query, re.UNICODE)`, double-quoted, and
joined with ` OR `. A query with no word tokens is a clean 422, never a 500. See
[docs/testing.md](../testing.md).

## What would change our minds

**Recall visibly failing.** Not "embeddings would be better in principle" — a real,
reported case of an assistant unable to find something it had written, where stemming and
the schema endpoint did not rescue it. At that point the honest shape is FTS5 *plus* a
vector index, with both consulted and their results kept in separate sections for the same
reason fields and notes are separate now, rather than a replacement.
