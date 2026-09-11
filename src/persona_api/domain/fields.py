"""Structured attributes, whose keys the assistant invents.

A field is a key, a required description, and a JSON value. All three of those words
carry a decision.

## The key is normalized, never rejected for style

``"Favourite Topics"``, ``"favourite-topics"`` and ``"FAVOURITE_TOPICS"`` are one field.
That is the whole anti-sprawl mechanism, and it works by making reuse cheap rather than
by refusing writes: an assistant that gets an error for a key it just invented will
invent a different one, and now there are two fields holding one fact. Only what
normalization genuinely cannot rescue -- nothing left after folding, a key that starts
with a digit, a key too long to read -- is refused.

## The description is required, and that is what fights sprawl

Nothing here enforces that two descriptions differ, because nothing could. The
description exists so the *next* write can tell whether to reuse this key: ``/schema``
lists keys with their descriptions precisely so an assistant can look before it invents.

## The value type is derived, never supplied

A caller that sends both a value and its type has two things that can disagree, and the
one that is wrong will be the one used for filtering. :func:`derive_value_type` is the
only source of a ``value_type``, and it checks ``bool`` before ``int`` because
``isinstance(True, int)`` is True in Python -- getting that order wrong types every
boolean in the database as a number, silently, forever.

## Every limit says which limit it was

:func:`validate_value` refuses with a message naming the limit that failed -- bytes,
depth, list items, object keys, or the type itself. A caller told only "invalid" has to
bisect its own payload to find out what to send instead, and an assistant doing that
will try five times and then give up and write a note instead.

## The shape is deliberately shallow

A scalar, a list of scalars, or a one-level object whose values are scalars or lists of
scalars. Nothing deeper, even when ``max_depth`` would allow it. A persona is not a
document store: arbitrary nesting is a thing you cannot search, cannot render into a
prompt, and cannot merge -- and the first person to store a nested blob here would be
storing the thing that ought to have been three fields.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from persona_api.domain.errors import InvalidFieldKeyError, InvalidFieldValueError

if TYPE_CHECKING:
    from datetime import datetime

    from persona_api.domain.provenance import Source

FIELD_ID_PREFIX = "fld_"

MAX_KEY_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 200

_SEPARATORS = re.compile(r"[\s-]+")
"""Whitespace and hyphens both mean "word boundary" to somebody naming a field."""

_REPEATED_UNDERSCORE = re.compile(r"_+")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
"""Snake case, starting with a letter.

Starting with a letter rather than a digit is not cosmetic: these keys become attribute
names in whatever renders a persona into a prompt, and ``2fa_enabled`` is not one.
"""


class ValueType(StrEnum):
    """The JSON shape of a field's value, as derived from the value itself.

    Stored alongside the value so that listing and filtering never have to parse every
    row's JSON to find out which rows are lists.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"
    LIST = "list"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class ValueLimits:
    """What a field value may not exceed.

    Passed in rather than read from configuration, because the domain reads no
    configuration. The defaults match ``core.config``'s, so a test that does not care
    about limits does not have to build a settings object to get realistic ones.
    """

    max_bytes: int = 4096
    """Measured on the serialized JSON, in UTF-8 bytes rather than characters.

    Bytes because that is what the row costs and what the prompt costs. A limit counted
    in characters would let one persona of CJK text be three times the size of another.
    """

    max_depth: int = 3
    """Container nesting. A bare scalar is depth 1, a list of scalars is depth 2."""

    max_list_items: int = 100
    max_object_keys: int = 50


def new_field_id() -> str:
    """Return a fresh field id.

    The id identifies the row; the *key* identifies the field within its persona and is
    what every API path uses. They differ because a key can be forgotten and written
    again, and the two rows are not the same row.
    """
    return f"{FIELD_ID_PREFIX}{uuid.uuid4().hex}"


def normalize_key(raw: str) -> str:
    """Fold a caller's key onto the one form this service stores.

    Lowercased, trimmed, runs of whitespace and hyphens turned into a single
    underscore, repeated underscores collapsed, and leading and trailing underscores
    removed -- so every plausible spelling of one field reaches the same row.

    Args:
        raw: The key as the caller wrote it.

    Returns:
        The normalized snake_case key.

    Raises:
        InvalidFieldKeyError: if nothing usable survives normalization, the key does not
            start with a letter, or it is longer than
            :data:`MAX_KEY_LENGTH` characters.
    """
    folded = _SEPARATORS.sub("_", raw.strip().lower())
    candidate = _REPEATED_UNDERSCORE.sub("_", folded).strip("_")
    if not candidate:
        msg = "a field key must contain something other than punctuation and spaces"
        raise InvalidFieldKeyError(msg)
    if len(candidate) > MAX_KEY_LENGTH:
        msg = (
            f"a field key is at most {MAX_KEY_LENGTH} characters, and this one is {len(candidate)}"
        )
        raise InvalidFieldKeyError(msg)
    if not _KEY.match(candidate):
        msg = (
            "a field key must start with a letter and hold only lowercase letters, "
            "digits and underscores"
        )
        raise InvalidFieldKeyError(msg)
    return candidate


def normalize_description(raw: str) -> str:
    """Trim a field description and refuse one that is missing.

    Required on create, and required to be non-empty, because the description is the
    only thing that makes a key reusable by somebody -- or something -- that did not
    invent it.

    Args:
        raw: The description as the caller wrote it.

    Returns:
        The trimmed description.

    Raises:
        InvalidFieldKeyError: if it is blank or longer than
            :data:`MAX_DESCRIPTION_LENGTH` characters. The key error rather than the
            value error on purpose: a description describes the key, and a caller
            fixing it is fixing how the key will be found again.
    """
    description = raw.strip()
    if not description:
        msg = "a field needs a description: it is what tells the next write to reuse this key"
        raise InvalidFieldKeyError(msg)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        msg = (
            f"a field description is at most {MAX_DESCRIPTION_LENGTH} characters, "
            f"and this one is {len(description)}"
        )
        raise InvalidFieldKeyError(msg)
    return description


def derive_value_type(value: object) -> ValueType:
    """Name the JSON shape of a value.

    The ``bool`` check comes before the numeric one deliberately. ``isinstance(True,
    int)`` is True in Python, so a numeric check placed first would type every boolean
    in the database as a number -- a filter for "the boolean fields" would return
    nothing, and the mistake would be invisible until somebody asked for one.

    Args:
        value: Any decoded JSON value.

    Returns:
        The matching :class:`ValueType`.

    Raises:
        InvalidFieldValueError: if the value is not a JSON type at all.
    """
    if value is None:
        return ValueType.NULL
    if isinstance(value, bool):
        return ValueType.BOOLEAN
    if isinstance(value, (int, float)):
        return ValueType.NUMBER
    if isinstance(value, str):
        return ValueType.STRING
    if isinstance(value, list):
        return ValueType.LIST
    if isinstance(value, dict):
        return ValueType.OBJECT
    msg = f"type: a field value cannot be a {type(value).__name__}"
    raise InvalidFieldValueError(msg)


def validate_value(value: object, *, limits: ValueLimits) -> str:
    """Check a value against every limit and return the JSON that will be stored.

    The serialization is part of the contract rather than a detail: keys are sorted and
    the separators are compact, so the same value always produces the same bytes. Two
    writes of the same value therefore produce the same row, which is what lets an
    "unchanged" write be recognised as one instead of bumping a revision.

    Args:
        value: Any decoded JSON value.
        limits: The ceilings to enforce.

    Returns:
        The canonical JSON encoding of the value.

    Raises:
        InvalidFieldValueError: naming the limit that failed -- ``type``, ``depth``,
            ``list items``, ``object keys`` or ``bytes``.
    """
    _check(value, limits=limits, depth=1, where="the value")
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    size = len(rendered.encode("utf-8"))
    if size > limits.max_bytes:
        msg = f"bytes: the value serializes to {size} bytes, over the limit of {limits.max_bytes}"
        raise InvalidFieldValueError(msg)
    return rendered


def flatten_value(value: object) -> str:
    """Render a value as the readable words that go into the search index.

    The full-text index holds this projection rather than the JSON, so that searching
    for a word inside a list value finds it without the tokenizer ever meeting a brace,
    a bracket or a quote. ``["jazz", "ambient"]`` indexes as ``jazz ambient``;
    ``{"city": "Lisbon"}`` indexes as ``city Lisbon``; ``None`` indexes as nothing at
    all, because a null value has no words in it and a literal ``null`` in the index
    would make every null field a hit for the query "null".

    Args:
        value: Any decoded JSON value.

    Returns:
        The words, space-separated. Possibly empty.
    """
    return " ".join(_words(value))


def _words(value: object) -> list[str]:
    """The searchable words of a value, in order, with nothing empty among them."""
    if value is None:
        return []
    if isinstance(value, bool):
        # The JSON spelling, so that a search for "true" finds a flag that is set.
        return ["true" if value else "false"]
    if isinstance(value, list):
        words: list[str] = []
        for item in value:
            words.extend(_words(item))
        return words
    if isinstance(value, dict):
        words = []
        for key, item in value.items():
            # The key is indexed too: "city Lisbon" makes both the question and the
            # answer findable, and a persona is searched with half-remembered words.
            words.append(str(key))
            words.extend(_words(item))
        return words
    text = str(value)
    return [text] if text else []


def _check(value: object, *, limits: ValueLimits, depth: int, where: str) -> None:
    """Check one node of a value, and everything inside it.

    ``where`` names the position -- ``the value``, ``the value['city']`` -- so a refusal
    points at the part of the payload to change rather than at the payload.
    """
    if depth > limits.max_depth:
        msg = f"depth: {where} is nested {depth} levels deep, over the limit of {limits.max_depth}"
        raise InvalidFieldValueError(msg)
    if isinstance(value, dict):
        _check_object(value, limits=limits, depth=depth, where=where)
    elif isinstance(value, list):
        _check_list(value, limits=limits, depth=depth, where=where)
    else:
        _check_scalar(value, where=where)


def _check_object(obj: dict[Any, Any], *, limits: ValueLimits, depth: int, where: str) -> None:
    """An object may hold scalars and lists of scalars, under string keys."""
    if len(obj) > limits.max_object_keys:
        msg = (
            f"object keys: {where} has {len(obj)} keys, over the limit of {limits.max_object_keys}"
        )
        raise InvalidFieldValueError(msg)
    for key, item in obj.items():
        if not isinstance(key, str):
            msg = f"type: {where} has a {type(key).__name__} key; object keys must be strings"
            raise InvalidFieldValueError(msg)
        inside = f"{where}[{key!r}]"
        if isinstance(item, dict):
            msg = (
                f"type: {inside} is an object; a field value nests one level, so an "
                "entry may be a scalar or a list of scalars but not another object"
            )
            raise InvalidFieldValueError(msg)
        _check(item, limits=limits, depth=depth + 1, where=inside)


def _check_list(items: list[Any], *, limits: ValueLimits, depth: int, where: str) -> None:
    """A list may hold scalars, and nothing else."""
    if len(items) > limits.max_list_items:
        msg = (
            f"list items: {where} has {len(items)} items, over the limit of {limits.max_list_items}"
        )
        raise InvalidFieldValueError(msg)
    for index, item in enumerate(items):
        inside = f"{where}[{index}]"
        if isinstance(item, (dict, list)):
            msg = (
                f"type: {inside} is a {type(item).__name__}; a list value holds scalars, "
                "because a list of containers is three fields wearing a trenchcoat"
            )
            raise InvalidFieldValueError(msg)
        _check(item, limits=limits, depth=depth + 1, where=inside)


def _check_scalar(value: object, *, where: str) -> None:
    """A leaf must be something JSON can carry and SQLite can hold."""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            # json.dumps would happily write NaN or Infinity, neither of which is JSON.
            # The row would then fail to parse in any client that is not Python.
            msg = f"type: {where} is not a finite number"
            raise InvalidFieldValueError(msg)
        return
    msg = (
        f"type: {where} is a {type(value).__name__}, which is not a JSON value; "
        "send a string, a number, a boolean, null, a list or an object"
    )
    raise InvalidFieldValueError(msg)


@dataclass(frozen=True, slots=True)
class Field:
    """One structured attribute of a persona.

    ``value`` is the decoded value and ``value_type`` is derived from it, so the two
    cannot disagree -- see :func:`derive_value_type`.

    ``forgotten_at`` is a tombstone, as it is for a note: forgetting a field hides it
    and ``include_forgotten=true`` brings it back. ``revision`` counts the times the
    value has changed, which is what lets an event say a field was revised without the
    event ever carrying the value.
    """

    field_id: str
    account_id: str
    profile: str
    key: str
    description: str
    value: object
    value_type: ValueType
    source: Source
    asserted_by: str
    pinned: bool
    revision: int
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None
