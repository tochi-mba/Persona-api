"""Turning what somebody typed into something FTS5 will accept.

FTS5's ``MATCH`` operator takes a **query language**, not a string of words. It has
operators (``AND``, ``OR``, ``NOT``, ``NEAR``), phrase quoting, prefix stars and column
filters -- and raw user input lands straight in it. Measured on this exact stack, before
this module existed:

    'answers"'     -> OperationalError: unterminated string
    'concise OR'   -> OperationalError: fts5: syntax error near ""
    'NEAR('        -> OperationalError: fts5: syntax error near ""
    '*'            -> OperationalError: unknown special query
    ''             -> OperationalError: fts5: syntax error near ""

Eight of ten plausible search strings were a 500. Not an injection -- there is no way to
reach another account's rows through ``MATCH``, because the account filter lives in the
``JOIN`` -- but a crash, on the endpoint a model is most likely to call with whatever the
person just said.

## The fix

Extract word tokens, double-quote each one, join with ``OR``. That is all.

    x AND OR y   ->   "x" OR "AND" OR "OR" OR "y"

Quoting turns every token into a literal phrase, so an operator somebody typed becomes a
word to search for rather than an operator -- which is almost always what they meant. A
query with no word tokens at all is a clean 422 rather than a crash, because "no matches"
and "you did not ask for anything" are different answers and a model reading the first
will try harder against a query that can never match.

## Why OR rather than AND

A persona is searched with half-remembered words. ``AND`` returns nothing unless every
word is present, which for "that thing about rust and the review" is nothing; ``OR``
returns the plausible rows and lets ``bm25()`` sort them, which is what ranking is for.

## What this gives up

Deliberate operators. Somebody who knows FTS5 cannot write ``voice NEAR/3 dry`` here and
have it mean anything. That is the cost of the endpoint never being a 500, and it is the
right way round: the operators are unavailable to the person who knew about them, rather
than the endpoint being unavailable to everybody else.
"""

from __future__ import annotations

import re

from persona_api.domain.errors import InvalidSearchError

WORD = re.compile(r"[^\W_]+", re.UNICODE)
r"""Runs of word characters, underscores excluded.

``\w`` includes the underscore, which would keep ``__init__`` together as one token;
here the underscore is a separator, because a field key is snake_case and somebody
searching for "forms of address" should find ``forms_of_address``.

Unicode-aware, so a persona written in Japanese is searchable rather than tokenising to
nothing and raising.
"""


def to_match_query(raw: str) -> str:
    """Render a caller's search string as an FTS5 ``MATCH`` expression.

    Args:
        raw: whatever the caller typed.

    Returns:
        A quoted ``OR`` expression that FTS5 will parse.

    Raises:
        InvalidSearchError: if there is not one word token in the input. That is an
            answer rather than an empty result: a caller told "no matches" will retry
            with a query that can never match, and a caller told "there was nothing to
            search for" will send words.
    """
    tokens = WORD.findall(raw)
    if not tokens:
        msg = "a search needs at least one word in it"
        raise InvalidSearchError(msg)
    # The tokens contain no quote characters -- the pattern cannot match one -- so
    # quoting them cannot produce an unterminated string, which is the failure that
    # started this module.
    return " OR ".join(f'"{token}"' for token in tokens)
