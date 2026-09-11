# ADR-0004: Provenance is partly a claim, and the API says which part it verified

**Status:** accepted.

## Context

[ADR-0001](0001-data-not-instructions.md) says a persona is data and must be rendered with
its provenance attached, so a reader can discount it. That only works if the provenance
means something — and it is very easy to ship a provenance field that means nothing at all
while looking authoritative.

The question a reader actually has is: *did a person say this, or did a model infer it?*
A thing the person said about themselves deserves more weight than a thing the assistant
guessed after reading a web page.

**The server cannot answer that question.** It sees an HTTP request carrying a bearer
token. It cannot see whether a human typed the words, whether a model inferred them, or
whether they were copied out of a document the model had just been shown. Any field
claiming to answer it is claiming something the server did not check.

## Decision

Two fields, kept separate on purpose, and the schema descriptions say which is which.

**`asserted_by` — server-derived. Trustworthy.**
The `aud` of the verified token that made the write. It is read from the verified claims
and **never from the request body**; a body field named `asserted_by` is ignored, and
there is a test that sets one and asserts it had no effect. It answers "which service's
token wrote this", which is a question the server genuinely knows the answer to.

**`source` — a claim. Labelled as one.**
One of `owner` | `assistant` | `service`: who the writer *says* asserted it. The server
records it faithfully and verifies nothing about it. Its schema description says so in as
many words, so a model reading the tool definition is told the field's epistemic status at
the same moment it is told the field exists.

## Why not drop `source`, since it is unverifiable

Because it is still the most useful signal available, and the alternative is worse.

A well-behaved client marks `owner` when the person typed it and `assistant` when the
model inferred it, and that distinction is exactly what a reader wants. A misbehaving
client can lie — but a misbehaving client could also just write whatever it wanted into
the note body. `source` does not widen the attack surface; it narrows the *honest* case
into something a reader can use.

What would be wrong is presenting it as verified. The whole cost of an unverifiable field
is paid at the moment somebody trusts it, so the mitigation is entirely in the labelling.

## Why not verify it, somehow

There is nothing to verify against. keyring's token carries `iss`, `sub`, `aud`, `iat`,
`exp` and nothing else — no roles, no scopes, no indication of whether a human was
present. Adding a "human was present" claim to keyring would be adding an unverifiable
field one service further away, which is the same problem with an extra network hop.

## What it costs

A reader who does not read the schema description will over-trust `source`. That is real,
and it is why the same distinction is repeated in three places: the field description in
the OpenAPI schema, [docs/api.md](../api.md), and [docs/mcp.md](../mcp.md) — which tells
the MCP layer to render `source` as "recorded as" and `asserted_by` as "written by".

## What would change our minds

A signed assertion from something that genuinely knows — a client attestation, or a
keyring claim that a human authenticated interactively within the last N seconds. Then
`source` could gain a fourth, verified value, and the honest thing would be to keep the
other three exactly as unverifiable as they are now rather than to retroactively promote
them.
