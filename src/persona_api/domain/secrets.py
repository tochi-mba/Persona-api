"""Refusing credential-shaped text.

A persona is not a vault. Somebody will eventually say "remember my OpenAI key", and the
honest answer is the vault next door -- so every piece of caller text that this service
stores goes through :func:`refuse_if_credential` first, and **there is no setting that
turns it off** (``docs/adr/0002-no-secrets.md``).

The reason it matters is not that a persona is a bad place to keep a secret, though it
is. It is that a persona is a *findable* place: field values are full-text indexed, notes
are returned in recall, and an assistant that stored a key here would cheerfully read it
back into a prompt six months later. A credential that reaches this database has to be
treated as disclosed, and the only reliable moment to stop that is the write.

## The heuristic is deliberately conservative, and asymmetric on purpose

There are two ways to be wrong and they do not cost the same:

* A **false negative** -- a credential that is stored -- is bad, and is why the prefix
  rules below exist for the shapes that are unmistakable.
* A **false positive** -- an ordinary memory that is refused -- is worse in the way that
  matters, because **there is no override**. A caller told "no" has no flag, no header
  and no admin to appeal to; the memory is simply unwritable, and the assistant will
  keep trying and keep failing.

So every rule here is written to refuse a *shape* rather than a *subject*. "Her password
manager is 1Password" is a sentence about credentials and is stored. ``AKIA...`` is not a
sentence at all and is not.

## What is detected

**Known prefixes.** ``sk-``, ``ghp_``, ``github_pat_``, ``xoxb-``/``xoxa-``/``xoxp-``/
``xoxs-``/``xoxr-``, ``AKIA``, ``AIza``, and a PEM private key block. Each requires a
plausible body after the prefix, so "the sk- prefix is how OpenAI keys start" is a
sentence about a prefix rather than a key.

**JWT shape.** Three base64url segments separated by dots, where the first begins
``eyJ``. That last requirement is load-bearing rather than decorative: a JWT header is
base64url of a JSON object, so it always begins ``eyJ``, while a bare three-dotted run
also describes ``persona_api.domain.secrets`` and every other module path anybody might
mention in a note.

**A long, space-free, high-entropy run**, which is the catch-all for the keys that have
no recognisable prefix. It refuses a run of 32 characters or more when, in one sentence:
*every character is from one alphabet -- hex, or base64url -- the run mixes letters with
digits, and it uses close to as many distinct characters as a run that length could.*

Each clause is there to spare a specific kind of ordinary text:

* *One alphabet* spares URLs and file paths, whose ``/``, ``:`` and ``.`` belong to no
  key alphabet. It is also why standard base64's ``+`` and ``/`` are **not** accepted
  here: those characters are the punctuation of paths and query strings, and including
  them would put every long URL one diversity check away from being refused. The cost is
  a base64 secret that happens to contain ``+`` or ``/`` and has no known prefix, which
  is the false negative we choose.
* *Letters and digits together* spares long words. A German compound noun, a hyphenated
  slug and a run of emoji are each all one kind of character and are never refused.
* *Close to as many distinct characters as it could have* spares structured identifiers.
  A UUID, ``screenshot-2026-03-14-at-11-42-07-am`` and a dotted version string all repeat
  themselves far more than random key material does.

The one ordinary thing this catches is a **full-length git sha**: forty hex characters is
a hash, and nothing here can tell a hash from a token. The abbreviated shas people
actually write -- ``9f8e7d6c`` -- are far under the length floor, so the tradeoff was
made in that direction knowingly.

## The two corpora are the specification

``tests/unit/domain/test_secrets.py`` holds ``MUST_BE_ACCEPTED`` and
``MUST_BE_REFUSED``: ordinary sentences somebody would genuinely want remembered, and
credential shapes. Those lists are what defines "conservative" here, and tuning this
module means making both pass -- never deleting an entry from either one.
"""

from __future__ import annotations

import re
import string

from persona_api.domain.errors import CredentialRefusedError

KEYRING_ADVICE = "store it in keyring instead; this service refuses credentials"
"""Said in every refusal.

Somebody who just tried to save an API key needs to be told where it goes, not merely
told no -- an assistant that is only refused will try a different wording, and one that
is redirected will call the other service.
"""

MIN_RUN_LENGTH = 32
"""The shortest space-free run the entropy rule will look at.

Under this, the false positives are everything: an order id, a short hash, a UUID
fragment, a long word. Real key material is longer than this essentially always, because
the thing generating it wanted 128 bits or more.
"""

MIN_DISTINCT_RATIO = 0.6
"""How much of a run's possible variety it must actually use, to count as key material.

Measured as distinct characters over ``min(length, alphabet size)`` -- the most distinct
characters a run that long *could* have had. Random base64 lands near 0.75 and random hex
near 0.9, while the structured identifiers that make up most long space-free text sit
well under 0.5 because they repeat their separators and their vocabulary.
"""

_HEX_ALPHABET_SIZE = 16
_BASE64URL_ALPHABET_SIZE = 64

_HEX = frozenset(string.hexdigits)
_BASE64URL = frozenset(string.ascii_letters + string.digits + "-_=")

_TRIM = "\"'`.,;:!?()[]{}<>"
"""Punctuation stripped from a run before it is judged.

A key pasted at the end of a sentence has a full stop stuck to it, and that full stop
would otherwise fail the alphabet test and let the key through.
"""

_PREFIXED: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
        "it starts with 'sk-', the OpenAI secret key prefix",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
        "it starts with 'github_pat_', the GitHub fine-grained token prefix",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}"),
        "it starts with 'ghp_', the GitHub personal access token prefix",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])xox[bapsr]-[A-Za-z0-9-]{10,}"),
        "it starts with 'xox', the Slack token prefix",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{12,}"),
        "it starts with 'AKIA', the AWS access key id prefix",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{20,}"),
        "it starts with 'AIza', the Google API key prefix",
    ),
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        "it contains a PEM private key block",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{13,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
        ),
        "it has the shape of a JWT: three base64url segments and a JSON header",
    ),
)
"""Shapes that are never anything but a credential, in the order they are tried.

Each reason names the *prefix*, never the text it was found in. A refusal that quoted the
value back would put the credential into the error response, the client's logs and quite
possibly the next prompt -- which is the disclosure this whole module exists to prevent.
"""

_RUNS = re.compile(r"\S+")


def looks_like_a_credential(text: str) -> str | None:
    """Judge whether some text is credential-shaped.

    Args:
        text: Any caller-supplied text -- a note body, a field value rendered to a
            string, a display name.

    Returns:
        The reason it was refused, phrased to be shown to the caller and containing none
        of the text itself, or ``None`` if nothing matched.
    """
    for pattern, reason in _PREFIXED:
        if pattern.search(text):
            return reason
    for run in _RUNS.findall(text):
        dense = _entropy_reason(run.strip(_TRIM))
        if dense is not None:
            return dense
    return None


def refuse_if_credential(text: str) -> None:
    """Raise if some text is credential-shaped, saying where it should have gone.

    Args:
        text: Any caller-supplied text.

    Raises:
        CredentialRefusedError: naming keyring, and carrying the matched rule as
            ``reason``.
    """
    reason = looks_like_a_credential(text)
    if reason is None:
        return
    msg = f"this looks like a credential ({reason}); {KEYRING_ADVICE}"
    raise CredentialRefusedError(msg, reason=reason)


def _entropy_reason(run: str) -> str | None:
    """Whether one space-free run is dense enough to be key material.

    See the module docstring: one alphabet, letters mixed with digits, and close to as
    many distinct characters as a run that length could have.
    """
    if len(run) < MIN_RUN_LENGTH:
        return None
    used = set(run)
    if used <= _HEX:
        alphabet_size = _HEX_ALPHABET_SIZE
        alphabet = "hexadecimal"
    elif used <= _BASE64URL:
        alphabet_size = _BASE64URL_ALPHABET_SIZE
        alphabet = "base64url"
    else:
        return None
    if not (any(c.isalpha() for c in run) and any(c.isdigit() for c in run)):
        # An ordinary long word is all letters and a year is all digits. Only something
        # generated mixes the two across thirty-odd characters.
        return None
    if len(used) / min(len(run), alphabet_size) < MIN_DISTINCT_RATIO:
        return None
    return (
        f"it contains a {len(run)}-character {alphabet} run with no spaces in it, "
        "which is the shape of a key rather than of a sentence"
    )
