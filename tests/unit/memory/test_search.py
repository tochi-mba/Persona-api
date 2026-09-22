"""Hostile search strings, and the promise that none of them is a 500."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from persona_api.domain.errors import InvalidSearchError
from persona_api.memory.search import to_match_query

HOSTILE = [
    # The ten measured on this stack before the sanitiser existed. Eight raised
    # OperationalError out of FTS5 -- not an injection, because the account filter is in
    # the JOIN, but a crash on the endpoint a model calls with whatever somebody said.
    'answers"',
    "concise OR",
    "NEAR(",
    "*",
    "",
    "x AND OR y",
    '"quoted phrase"',
    "voice NEAR/3 dry",
    "^caret",
    "col:filter",
    # And more of the same shape, because the ten were never the whole set.
    "(((",
    "AND",
    "___",
    "   ",
    "-negate",
    "a AND NOT b",
    "prefer*",
    "it's",
    "簡潔",
    "{}[]",
]


@pytest.fixture
def index() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='porter unicode61')")
    connection.execute("INSERT INTO t(text) VALUES ('I prefer concise answers and dry humour')")
    try:
        yield connection
    finally:
        # Closed rather than left for the collector: Python 3.13 reports an unclosed
        # connection as a ResourceWarning, and this suite treats warnings as failures.
        connection.close()


class TestNothingIsEverAFiveHundred:
    @pytest.mark.parametrize("query", HOSTILE)
    def test_a_hostile_string_is_a_clean_answer_or_a_clean_refusal(
        self, query: str, index: sqlite3.Connection
    ) -> None:
        # The whole property: every one of these either becomes a query FTS5 parses, or
        # a 422 saying there was nothing to search for. Never an OperationalError
        # escaping into a handler as a 500.
        try:
            match = to_match_query(query)
        except InvalidSearchError:
            return

        index.execute("SELECT rowid FROM t WHERE t MATCH ? ORDER BY bm25(t)", (match,)).fetchall()


class TestTokenising:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("concise", '"concise"'),
            ("dry humour", '"dry" OR "humour"'),
            ("x AND OR y", '"x" OR "AND" OR "OR" OR "y"'),
            # An operator somebody typed becomes a word to search for, which is almost
            # always what they meant.
            ("NEAR(", '"NEAR"'),
            ("prefer*", '"prefer"'),
            # The underscore is a separator here, not a word character, because a field
            # key is snake_case and somebody searching "forms of address" should find
            # forms_of_address.
            ("forms_of_address", '"forms" OR "of" OR "address"'),
            ("簡潔", '"簡潔"'),
        ],
    )
    def test_it_quotes_each_word_and_joins_with_or(self, query: str, expected: str) -> None:
        assert to_match_query(query) == expected

    def test_or_rather_than_and_because_a_persona_is_searched_from_memory(self) -> None:
        # AND returns nothing unless every word is present, which for "that thing about
        # rust and the review" is nothing. OR returns the plausible rows and lets bm25
        # sort them, which is what ranking is for.
        assert " OR " in to_match_query("rust review thing")
        assert " AND " not in to_match_query("rust review thing")

    def test_a_quote_can_never_reach_the_query(self) -> None:
        # The failure that started this module was an unterminated string. The token
        # pattern cannot match a quote character, so quoting the tokens cannot produce
        # one.
        assert '"' not in to_match_query('answers"').replace('"', "", 2)


class TestNothingToSearchFor:
    @pytest.mark.parametrize("query", ["", "   ", "***", "___", "(((", "!!!"])
    def test_a_query_with_no_words_is_refused_rather_than_returning_nothing(
        self, query: str
    ) -> None:
        # An answer rather than an empty result: a caller told "no matches" will retry
        # with a query that can never match, and a caller told "there was nothing to
        # search for" will send words.
        with pytest.raises(InvalidSearchError):
            to_match_query(query)
