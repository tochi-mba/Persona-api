"""Key normalization, value typing, and every limit that says which limit it was."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from persona_api.domain.errors import InvalidFieldKeyError, InvalidFieldValueError
from persona_api.domain.fields import (
    FIELD_ID_PREFIX,
    MAX_DESCRIPTION_LENGTH,
    MAX_KEY_LENGTH,
    Field,
    ValueLimits,
    ValueType,
    derive_value_type,
    flatten_value,
    new_field_id,
    normalize_description,
    normalize_key,
    validate_value,
)
from persona_api.domain.provenance import Source

SPRAWL = [
    "favourite_topics",
    "Favourite Topics",
    "favourite-topics",
    "FAVOURITE_TOPICS",
    "  favourite   topics  ",
    "favourite__topics",
    "--favourite-topics--",
    "Favourite   -   Topics",
    "_favourite_topics_",
]
"""Every plausible spelling of one field.

This table is the anti-sprawl mechanism, and it fails by being *almost* right: one of
these mapping somewhere else means a persona holding the same fact under two keys, with
nothing anywhere to say so.
"""


class TestKeyNormalization:
    @pytest.mark.parametrize("written", SPRAWL)
    def test_every_spelling_of_one_field_collapses_onto_one_key(self, written: str) -> None:
        assert normalize_key(written) == "favourite_topics"

    def test_the_whole_sprawl_table_is_a_single_key(self) -> None:
        # Stated as a set rather than one assertion per row, because the property is
        # "there is one key here", not "each row maps somewhere".
        assert {normalize_key(written) for written in SPRAWL} == {"favourite_topics"}

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("voice", "voice"),
            ("things I got wrong", "things_i_got_wrong"),
            ("forms-of-address", "forms_of_address"),
            ("Tone\tOf\nVoice", "tone_of_voice"),
            ("a", "a"),
        ],
    )
    def test_it_folds_the_ordinary_cases(self, written: str, expected: str) -> None:
        assert normalize_key(written) == expected

    @pytest.mark.parametrize("written", ["", "   ", "___", "- - -", "\t\n"])
    def test_a_key_with_nothing_in_it_is_refused(self, written: str) -> None:
        # Normalization rescues what it can; this is what it cannot.
        with pytest.raises(InvalidFieldKeyError):
            normalize_key(written)

    @pytest.mark.parametrize("written", ["2fa_enabled", "3_strikes", "9lives"])
    def test_a_key_starting_with_a_digit_is_refused(self, written: str) -> None:
        # These become attribute names in whatever renders a persona into a prompt,
        # and `2fa_enabled` is not one.
        with pytest.raises(InvalidFieldKeyError):
            normalize_key(written)

    def test_a_key_that_normalizes_to_punctuation_is_refused(self) -> None:
        with pytest.raises(InvalidFieldKeyError):
            normalize_key("!!!")

    def test_a_key_too_long_to_read_is_refused_and_the_message_says_the_limit(self) -> None:
        with pytest.raises(InvalidFieldKeyError, match=str(MAX_KEY_LENGTH)):
            normalize_key("k" * (MAX_KEY_LENGTH + 1))

    def test_a_key_exactly_at_the_limit_is_kept(self) -> None:
        assert normalize_key("k" * MAX_KEY_LENGTH) == "k" * MAX_KEY_LENGTH


class TestDescriptions:
    def test_it_trims(self) -> None:
        assert normalize_description("  what this is for  ") == "what this is for"

    @pytest.mark.parametrize("written", ["", "   "])
    def test_a_missing_description_is_refused(self, written: str) -> None:
        # Required because it is the only thing that makes a key reusable by something
        # that did not invent it -- which is how key sprawl is actually fought.
        with pytest.raises(InvalidFieldKeyError, match="description"):
            normalize_description(written)

    def test_a_description_over_the_limit_is_refused(self) -> None:
        with pytest.raises(InvalidFieldKeyError, match=str(MAX_DESCRIPTION_LENGTH)):
            normalize_description("d" * (MAX_DESCRIPTION_LENGTH + 1))


class TestValueTypeDerivation:
    def test_a_boolean_is_a_boolean_and_not_a_number(self) -> None:
        # isinstance(True, int) is True in Python, so a numeric check placed first
        # would type every boolean in the database as a number -- silently, forever,
        # and invisibly until somebody filtered for the boolean fields and got none.
        assert derive_value_type(True) is ValueType.BOOLEAN
        assert derive_value_type(False) is ValueType.BOOLEAN

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ValueType.NULL),
            ("dry and concise", ValueType.STRING),
            (42, ValueType.NUMBER),
            (3.5, ValueType.NUMBER),
            (["jazz", "ambient"], ValueType.LIST),
            ({"city": "Lisbon"}, ValueType.OBJECT),
        ],
    )
    def test_it_names_the_json_shape(self, value: object, expected: ValueType) -> None:
        assert derive_value_type(value) is expected

    def test_something_that_is_not_json_at_all_is_refused(self) -> None:
        with pytest.raises(InvalidFieldValueError, match="type"):
            derive_value_type({1, 2, 3})


class TestValueLimits:
    """Each limit, just under and just over, with the message naming which one."""

    def test_a_value_at_the_byte_limit_is_kept(self) -> None:
        limits = ValueLimits(max_bytes=32)
        # Two quotes plus thirty characters is exactly thirty-two bytes.
        assert validate_value("x" * 30, limits=limits) == '"' + "x" * 30 + '"'

    def test_a_value_over_the_byte_limit_names_bytes(self) -> None:
        with pytest.raises(InvalidFieldValueError, match="bytes"):
            validate_value("x" * 31, limits=ValueLimits(max_bytes=32))

    def test_bytes_are_counted_in_utf8_not_characters(self) -> None:
        # A limit counted in characters would let one persona of CJK text be three
        # times the size of another, and the cost of a value is its bytes.
        with pytest.raises(InvalidFieldValueError, match="bytes"):
            validate_value("簡潔" * 6, limits=ValueLimits(max_bytes=32))

    def test_a_list_at_the_item_limit_is_kept(self) -> None:
        assert validate_value(list(range(3)), limits=ValueLimits(max_list_items=3))

    def test_a_list_over_the_item_limit_names_list_items(self) -> None:
        with pytest.raises(InvalidFieldValueError, match="list items"):
            validate_value(list(range(4)), limits=ValueLimits(max_list_items=3))

    def test_an_object_at_the_key_limit_is_kept(self) -> None:
        assert validate_value({"a": 1, "b": 2}, limits=ValueLimits(max_object_keys=2))

    def test_an_object_over_the_key_limit_names_object_keys(self) -> None:
        with pytest.raises(InvalidFieldValueError, match="object keys"):
            validate_value({"a": 1, "b": 2, "c": 3}, limits=ValueLimits(max_object_keys=2))

    def test_a_value_deeper_than_the_depth_limit_names_depth(self) -> None:
        # A bare scalar is depth 1 and a list of scalars is depth 2, so a depth of 1
        # admits scalars only.
        with pytest.raises(InvalidFieldValueError, match="depth"):
            validate_value(["a"], limits=ValueLimits(max_depth=1))

    def test_a_scalar_is_depth_one(self) -> None:
        assert validate_value("a", limits=ValueLimits(max_depth=1)) == '"a"'

    @pytest.mark.parametrize(
        "value",
        [
            [["nested"]],
            [{"nested": 1}],
            {"outer": {"inner": 1}},
        ],
    )
    def test_a_container_inside_a_container_is_refused(self, value: object) -> None:
        # A persona is not a document store. Arbitrary nesting is a thing you cannot
        # search, cannot render into a prompt, and cannot merge.
        with pytest.raises(InvalidFieldValueError, match="type"):
            validate_value(value, limits=ValueLimits())

    @pytest.mark.parametrize("value", [{1, 2}, datetime(2026, 1, 1, tzinfo=UTC), 1 + 2j])
    def test_something_json_cannot_carry_is_refused(self, value: object) -> None:
        with pytest.raises(InvalidFieldValueError, match="type"):
            validate_value(value, limits=ValueLimits())

    def test_a_non_string_object_key_is_refused(self) -> None:
        with pytest.raises(InvalidFieldValueError, match="type"):
            validate_value({1: "one"}, limits=ValueLimits())

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_number_json_cannot_spell_is_refused(self, value: float) -> None:
        # json.dumps would happily write NaN or Infinity, neither of which is JSON, and
        # the row would then fail to parse in any client that is not Python.
        with pytest.raises(InvalidFieldValueError, match="type"):
            validate_value(value, limits=ValueLimits())

    def test_a_refusal_points_at_the_offending_part_rather_than_the_payload(self) -> None:
        # A caller told only "invalid" has to bisect its own payload; an assistant
        # doing that will try five times and then give up and write a note instead.
        with pytest.raises(InvalidFieldValueError, match="city"):
            validate_value({"city": {"name": "Lisbon"}}, limits=ValueLimits())

    def test_the_serialization_is_canonical_so_an_unchanged_write_is_recognisable(
        self,
    ) -> None:
        # Same value, same bytes, regardless of how the caller ordered its keys --
        # which is what lets a re-PUT of the same value be seen as unchanged instead of
        # bumping a revision.
        first = validate_value({"b": 2, "a": 1}, limits=ValueLimits())
        second = validate_value({"a": 1, "b": 2}, limits=ValueLimits())

        assert first == second == '{"a":1,"b":2}'

    def test_non_ascii_survives_serialization_unescaped(self) -> None:
        assert validate_value("簡潔", limits=ValueLimits()) == '"簡潔"'


class TestFlattening:
    """The projection that goes into the search index."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("dry and concise", "dry and concise"),
            (["jazz", "ambient"], "jazz ambient"),
            ({"city": "Lisbon"}, "city Lisbon"),
            ({"cities": ["Lisbon", "Porto"]}, "cities Lisbon Porto"),
            (42, "42"),
            (True, "true"),
            (False, "false"),
            (None, ""),
            ([], ""),
            ("", ""),
        ],
    )
    def test_it_renders_the_words_and_nothing_else(self, value: object, expected: str) -> None:
        assert flatten_value(value) == expected

    @pytest.mark.parametrize("punctuation", ['"', "{", "}", "[", "]", ":", ","])
    def test_no_json_punctuation_reaches_the_index(self, punctuation: str) -> None:
        # The tokenizer must never meet a brace: a search for a word inside a list
        # value has to find it, and JSON punctuation would either split the token or
        # become one.
        rendered = flatten_value({"cities": ["Lisbon", "Porto"], "count": 2})

        assert punctuation not in rendered

    def test_a_null_value_contributes_no_words(self) -> None:
        # A literal "null" in the index would make every null field a hit for the
        # query "null", which is a search result nobody wants and nobody asked for.
        assert flatten_value({"pronouns": None}) == "pronouns"


class TestFieldIds:
    def test_an_id_is_opaque_and_prefixed(self) -> None:
        assert new_field_id().startswith(FIELD_ID_PREFIX)

    def test_two_ids_differ(self) -> None:
        assert new_field_id() != new_field_id()


class TestTheFieldType:
    def test_it_is_frozen_so_a_read_cannot_be_mutated_without_an_event(self) -> None:
        field = Field(
            field_id=new_field_id(),
            account_id="acct",
            profile="work",
            key="voice",
            description="how it speaks",
            value="dry",
            value_type=ValueType.STRING,
            source=Source.ASSISTANT,
            asserted_by="persona",
            pinned=False,
            revision=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            forgotten_at=None,
        )

        with pytest.raises(AttributeError):
            field.value = "loud"  # type: ignore[misc]
