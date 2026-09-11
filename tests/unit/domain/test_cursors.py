"""Cursors, and the one property that makes them worth having over an offset."""

from __future__ import annotations

import pytest

from persona_api.domain.cursors import decode_cursor, encode_cursor
from persona_api.domain.errors import InvalidCursorError


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("sort_key", "row_id"),
        [
            ("2026-03-01T12:00:00.000000+00:00", "note_abc123"),
            ("voice", "fld_abc123"),
            ("", ""),
            # A sort key holding the characters a datetime and a key can carry. The
            # separator is the ASCII unit separator precisely so none of these splits
            # the pair in the wrong place; a comma would.
            ("a,b:c-d.e+f", "note_x"),
            ("簡潔", "note_y"),
        ],
    )
    def test_a_cursor_recovers_exactly_what_it_encoded(self, sort_key: str, row_id: str) -> None:
        assert decode_cursor(encode_cursor(sort_key, row_id)) == (sort_key, row_id)

    def test_a_cursor_is_url_safe_so_it_survives_a_query_string(self) -> None:
        cursor = encode_cursor("2026-03-01T12:00:00.000000+00:00", "note_abc")

        assert "+" not in cursor
        assert "/" not in cursor
        assert "=" not in cursor

    def test_a_cursor_does_not_read_as_its_contents(self) -> None:
        # Opaque on purpose: a caller that can read a cursor is a caller that will
        # construct one, and the shape of the pair has to stay free to change.
        assert "note_abc" not in encode_cursor("2026-03-01", "note_abc")


class TestRefusal:
    @pytest.mark.parametrize(
        "forged",
        [
            "not base64 at all!!",
            "////",
            # Valid base64 of bytes that are not UTF-8.
            "__8",
            # Valid base64 of valid UTF-8 with no separator in it.
            "aGVsbG8",
        ],
    )
    def test_a_cursor_this_service_did_not_issue_is_refused(self, forged: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(forged)

    def test_every_way_of_being_wrong_gives_the_same_message(self) -> None:
        # A caller holding a cursor we did not mint has nothing to learn from which
        # kind of wrong it was, and distinguishing them would only describe the format.
        messages = set()
        for forged in ("not base64 at all!!", "aGVsbG8"):
            with pytest.raises(InvalidCursorError) as refusal:
                decode_cursor(forged)
            messages.add(str(refusal.value))

        assert len(messages) == 1
