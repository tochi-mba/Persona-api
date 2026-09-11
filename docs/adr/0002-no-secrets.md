# ADR-0002: No secrets in a persona; keyring is next door

**Status:** accepted.

## Context

People will paste an API key into "remember this". Not occasionally — routinely, and in
good faith, because "remember this for me" is exactly what a memory service offers and a
key is exactly the sort of thing that is annoying to keep finding.

An assistant will do it too, and with less hesitation, because it has just been handed a
token by a tool and recording it looks like diligence.

If it lands here it is in plaintext, in a file with no encryption at all, in a service
whose entire read surface is "give me everything you know" — the export endpoint, the
recall endpoint, the identity block that gets pasted into a prompt every turn. A
credential in a persona is a credential in every future prompt.

[keyring](https://github.com/tochi-mba/Keyring-api) is the sibling service, it is next
door, it does envelope encryption, and it exists for precisely this.

## Decision

`domain/secrets.py` inspects every field value and every note body before it is stored.
A value that looks like a credential is refused with **422 and a message naming keyring**.

**There is no setting that disables this.** Not an opt-out, not an override header, not a
`force=true`. The absence is the feature: a knob here would be turned on by the first
person who hit a false positive at an inconvenient moment, and then never turned off.

What is detected:

- **Known prefixes**, which are unambiguous: `sk-`, `ghp_`, `github_pat_`, `xox[bapsr]-`,
  `AKIA`, `AIza`, and a PEM `-----BEGIN … PRIVATE KEY-----` block.
- **JWT shape**: three base64url segments separated by dots.
- **A long, high-entropy, space-free run**: length ≥ 32, a base64/hex alphabet, and
  genuine character diversity.

## The heuristic is deliberately conservative

A false positive blocks a legitimate memory **and there is no override**, so the third
rule is the one that needed care. An ordinary long word is all letters and low diversity;
a credential mixes cases, digits and symbols. Requiring *both* alphabet membership *and*
digit-and-letter mixing is what keeps "Donaudampfschiffahrtsgesellschaft" and a long file
path out of the refusal set.

**The specification is two corpora, and both live in the test suite**:

- `MUST_BE_ACCEPTED` — sentences that must be stored, including ones containing a git sha,
  a URL, a hex colour, a long compound word, a file path, CJK text and emoji.
- `MUST_BE_REFUSED` — credential shapes, including ones embedded mid-sentence.

When the heuristic is tuned, it is tuned so both pass. **Never by deleting a corpus
entry.** An entry that genuinely cannot be classified gets a comment saying so, and stays.

## What it costs

Some legitimate value, somewhere, will be refused. That is the accepted cost, and the
error message is written to make it survivable: it says what was seen, never quotes the
value back, and names the service that should have it.

The reverse cost is worse and is why the trade goes this way: a credential that gets in is
not detected later. It is read back into a prompt, forever, by design.

## What would change our minds

Nothing about the rule. The corpora will grow — every false positive somebody reports
becomes a new `MUST_BE_ACCEPTED` entry and a tightening of the heuristic, which is the
mechanism by which this improves rather than a reason to add a switch.
